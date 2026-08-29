"""Recording access, playback signing and watch-progress regression cover.

Every test here corresponds to a finding in the 2026-08-23 Academy dashboard
audit (§3, §4, §9). The bugs, in the order they appear below:

  · SubjectRecordingsView had NO gate at all. active_batch_id() returns None
    for a non-enrolled caller, so the batch filter degraded to
    Q(batch__isnull=True) — every course-wide recording, bunny_video_id and
    all — for any authenticated account that could guess a subject UUID.
  · The two video-progress views never called _require_recording_viewer, so
    any account could read/write progress against any recording UUID, and
    rows were keyed on the ACCOUNT so siblings shared one watch position.
  · duration_seconds was never written by anything, so percent_complete was
    permanently null.
  · SaveRecordingView accepted a batch from a DIFFERENT course, producing a
    recording nobody could ever see.
  · Playback embed URLs were built client-side and were permanent.
"""
import hashlib
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, User, UserRole
from enrollments.models import Enrollment

from .models import Batch, Course, Subject, TeachingAssignment
from .models_progress import VideoProgress
from .models_recordings import SessionRecording


def _client(user, context, profile=None):
    c = APIClient()
    token = {"context": context}
    if profile is not None:
        token["active_profile"] = str(profile.id)
    c.force_authenticate(user=user, token=token)
    return c


class RecordingAccessBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        student_role, _ = Role.objects.get_or_create(name="STUDENT")

        cls.course = Course.objects.create(title="Class 10 Science")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.batch_a = Batch.objects.create(course=cls.course, name="Batch A", code="A")
        cls.batch_b = Batch.objects.create(course=cls.course, name="Batch B", code="B")

        # A batch belonging to a DIFFERENT course — the foreign-batch case.
        cls.other_course = Course.objects.create(title="Class 10 Maths")
        cls.other_subject = Subject.objects.create(
            course=cls.other_course, name="Algebra",
        )
        cls.foreign_batch = Batch.objects.create(
            course=cls.other_course, name="Maths Batch", code="M",
        )

        cls.teacher = User.objects.create_user(
            username="teach", email="teach@example.com", password="pw",
        )
        UserRole.objects.create(user=cls.teacher, role=teacher_role)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True,
        )

        # An enrolled learner, placed in Batch A.
        cls.parent = User.objects.create_user(
            username="parent", email="parent@example.com", password="pw",
        )
        UserRole.objects.create(user=cls.parent, role=student_role)
        cls.child_a = LearnerProfile.objects.create(
            account=cls.parent, display_name="Aarav", full_name="Aarav",
            is_default=True,
        )
        cls.child_b = LearnerProfile.objects.create(
            account=cls.parent, display_name="Bina", full_name="Bina",
        )
        Enrollment.objects.create(
            user=cls.parent, learner_profile=cls.child_a, course=cls.course,
            batch=cls.batch_a, status=Enrollment.STATUS_ACTIVE,
        )
        Enrollment.objects.create(
            user=cls.parent, learner_profile=cls.child_b, course=cls.course,
            batch=cls.batch_a, status=Enrollment.STATUS_ACTIVE,
        )

        # A fully authenticated OUTSIDER — not enrolled anywhere. This is the
        # account the CRITICAL finding was about.
        cls.outsider = User.objects.create_user(
            username="outsider", email="out@example.com", password="pw",
        )
        UserRole.objects.create(user=cls.outsider, role=student_role)
        cls.outsider_profile = LearnerProfile.objects.create(
            account=cls.outsider, display_name="Nobody", full_name="Nobody",
            is_default=True,
        )

        cls.rec_open = SessionRecording.objects.create(
            subject=cls.subject, batch=None, title="Course-wide lecture",
            bunny_video_id="vid-open", uploaded_by=cls.teacher, status=4,
            duration_seconds=600, is_published=True,
        )
        cls.rec_batch_a = SessionRecording.objects.create(
            subject=cls.subject, batch=cls.batch_a, title="Batch A only",
            bunny_video_id="vid-a", uploaded_by=cls.teacher, status=4,
            is_published=True,
        )
        cls.rec_batch_b = SessionRecording.objects.create(
            subject=cls.subject, batch=cls.batch_b, title="Batch B only",
            bunny_video_id="vid-b", uploaded_by=cls.teacher, status=4,
            is_published=True,
        )

    def list_url(self, subject=None):
        return f"/api/courses/subjects/{(subject or self.subject).id}/recordings/"


class SubjectRecordingsGateTest(RecordingAccessBase):
    """CRITICAL — SubjectRecordingsView had no enrolment check whatsoever."""

    def test_outsider_cannot_list_a_subjects_recordings(self):
        res = _client(self.outsider, "learner", self.outsider_profile).get(
            self.list_url()
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_outsider_does_not_receive_bunny_video_ids(self):
        res = _client(self.outsider, "learner", self.outsider_profile).get(
            self.list_url()
        )
        self.assertNotIn("vid-open", res.content.decode())

    def test_learner_with_no_active_profile_is_denied(self):
        # No active_profile claim at all — previously indistinguishable from
        # "enrolled but unbatched", which is what made the filter degrade.
        res = _client(self.parent, "learner").get(self.list_url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_enrolled_learner_sees_course_wide_plus_own_batch_only(self):
        res = _client(self.parent, "learner", self.child_a).get(self.list_url())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        titles = {r["title"] for r in res.json()}
        self.assertEqual(titles, {"Course-wide lecture", "Batch A only"})

    def test_assigned_teacher_sees_every_batch(self):
        res = _client(self.teacher, "teacher").get(self.list_url())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.json()), 3)

    def test_revoked_enrollment_loses_access(self):
        Enrollment.objects.filter(learner_profile=self.child_a).update(
            status=Enrollment.STATUS_REVOKED,
        )
        res = _client(self.parent, "learner", self.child_a).get(self.list_url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class VideoProgressGateTest(RecordingAccessBase):
    """HIGH — the progress endpoints were IsAuthenticated only."""

    def get_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/progress/"

    def save_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/progress/save/"

    def test_outsider_cannot_read_progress(self):
        res = _client(self.outsider, "learner", self.outsider_profile).get(
            self.get_url(self.rec_open)
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_outsider_cannot_write_progress(self):
        res = _client(self.outsider, "learner", self.outsider_profile).post(
            self.save_url(self.rec_open), {"last_position": 42}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(VideoProgress.objects.exists())

    def test_wrong_batch_learner_cannot_write_progress(self):
        # The list already hides Batch B's recording from this learner; the
        # per-id endpoints must agree, or they are a side door around it.
        res = _client(self.parent, "learner", self.child_a).post(
            self.save_url(self.rec_batch_b), {"last_position": 10}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_entitled_learner_can_write_and_read_back(self):
        c = _client(self.parent, "learner", self.child_a)
        res = c.post(self.save_url(self.rec_open), {"last_position": 120}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        res = c.get(self.get_url(self.rec_open))
        self.assertEqual(res.json()["last_position"], 120)
        # 120 / 600 = 20%. Null before duration_seconds was ever written.
        self.assertEqual(res.json()["percent_complete"], 20.0)


class VideoProgressProfileIsolationTest(RecordingAccessBase):
    """HIGH (theme T2) — rows were keyed on the ACCOUNT, so two children on
    one parent's email shared a single watch position."""

    def save_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/progress/save/"

    def get_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/progress/"

    def test_siblings_keep_separate_positions(self):
        _client(self.parent, "learner", self.child_a).post(
            self.save_url(self.rec_open), {"last_position": 300}, format="json",
        )
        _client(self.parent, "learner", self.child_b).post(
            self.save_url(self.rec_open), {"last_position": 45}, format="json",
        )

        a = _client(self.parent, "learner", self.child_a).get(self.get_url(self.rec_open))
        b = _client(self.parent, "learner", self.child_b).get(self.get_url(self.rec_open))
        self.assertEqual(a.json()["last_position"], 300)
        self.assertEqual(b.json()["last_position"], 45)
        self.assertEqual(VideoProgress.objects.count(), 2)

    def test_completion_does_not_leak_between_siblings(self):
        _client(self.parent, "learner", self.child_a).post(
            self.save_url(self.rec_open), {"last_position": 599}, format="json",
        )
        a = _client(self.parent, "learner", self.child_a).get(self.get_url(self.rec_open))
        b = _client(self.parent, "learner", self.child_b).get(self.get_url(self.rec_open))
        self.assertTrue(a.json()["completed"])
        self.assertFalse(b.json()["completed"])

    def test_teacher_row_is_account_keyed_and_does_not_collide(self):
        _client(self.parent, "learner", self.child_a).post(
            self.save_url(self.rec_open), {"last_position": 10}, format="json",
        )
        _client(self.teacher, "teacher").post(
            self.save_url(self.rec_open), {"last_position": 500}, format="json",
        )
        self.assertEqual(VideoProgress.objects.count(), 2)
        self.assertTrue(
            VideoProgress.objects.filter(
                student=self.teacher, learner_profile__isnull=True,
            ).exists()
        )

    def test_position_is_clamped_to_the_video_length(self):
        c = _client(self.parent, "learner", self.child_a)
        c.post(self.save_url(self.rec_open), {"last_position": 99999}, format="json")
        self.assertEqual(
            VideoProgress.objects.get(learner_profile=self.child_a).last_position,
            600.0,
        )


class SaveRecordingBatchValidationTest(RecordingAccessBase):
    """MEDIUM — a batch from another course was accepted, producing content
    no student could ever see."""

    def save_url(self, subject=None):
        return f"/api/courses/subjects/{(subject or self.subject).id}/recordings/save/"

    def test_foreign_course_batch_is_rejected(self):
        res = _client(self.teacher, "teacher").post(
            self.save_url(),
            {"title": "Orphan", "video_id": "vid-x",
             "batch_id": str(self.foreign_batch.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(SessionRecording.objects.filter(title="Orphan").exists())

    def test_own_course_batch_is_accepted(self):
        res = _client(self.teacher, "teacher").post(
            self.save_url(),
            {"title": "Real", "video_id": "vid-y",
             "batch_id": str(self.batch_a.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            SessionRecording.objects.get(title="Real").batch_id, self.batch_a.id,
        )

    def test_saving_a_recording_notifies_the_batch_that_can_see_it(self):
        from activity.models import Activity

        _client(self.teacher, "teacher").post(
            self.save_url(),
            {"title": "Notified", "video_id": "vid-n",
             "batch_id": str(self.batch_a.id)},
            format="json",
        )
        rows = Activity.objects.filter(type=Activity.TYPE_RECORDING)
        self.assertEqual(rows.count(), 2)  # both Batch A children
        self.assertTrue(rows.first().title.startswith("New recording:"))


class RecordingDurationCaptureTest(RecordingAccessBase):
    """HIGH — nothing ever wrote duration_seconds, so every recording read
    0% forever."""

    def status_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/status/"

    def _bunny(self, payload, code=200):
        class R:
            status_code = code

            def json(self):
                return payload

        return R()

    @override_settings(BUNNY_LIBRARY_ID="1", BUNNY_API_KEY="k", BUNNY_CDN_HOST="cdn.test")
    def test_length_is_persisted_from_the_bunny_payload(self):
        rec = SessionRecording.objects.create(
            subject=self.subject, title="Processing", bunny_video_id="vid-p",
            uploaded_by=self.teacher, status=2,
        )
        with patch("courses.views_recordings.requests.get") as g:
            g.return_value = self._bunny(
                {"status": 4, "length": 2731, "thumbnailFileName": "t.jpg"}
            )
            res = _client(self.teacher, "teacher").get(self.status_url(rec))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rec.refresh_from_db()
        self.assertEqual(rec.duration_seconds, 2731)
        self.assertEqual(rec.status, 4)

    @override_settings(BUNNY_LIBRARY_ID="1", BUNNY_API_KEY="k")
    def test_already_ready_recording_still_backfills_a_missing_duration(self):
        # The early return used to fire on status alone, so anything that
        # reached READY before duration capture existed could never get one.
        rec = SessionRecording.objects.create(
            subject=self.subject, title="Legacy ready", bunny_video_id="vid-l",
            uploaded_by=self.teacher, status=4, duration_seconds=None,
        )
        with patch("courses.views_recordings.requests.get") as g:
            g.return_value = self._bunny({"status": 4, "length": 900})
            _client(self.teacher, "teacher").get(self.status_url(rec))
        rec.refresh_from_db()
        self.assertEqual(rec.duration_seconds, 900)

    @override_settings(BUNNY_LIBRARY_ID="1", BUNNY_API_KEY="k")
    def test_ready_recording_with_a_duration_makes_no_bunny_call(self):
        with patch("courses.views_recordings.requests.get") as g:
            _client(self.teacher, "teacher").get(self.status_url(self.rec_open))
            g.assert_not_called()


@override_settings(BUNNY_LIBRARY_ID="4242")
class RecordingPlaybackTest(RecordingAccessBase):
    """HIGH — playback was an unauthenticated, permanent iframe URL the
    client built itself from a library id shipped in the bundle."""

    def url(self, rec):
        return f"/api/courses/recordings/{rec.id}/playback/"

    @override_settings(BUNNY_STREAM_TOKEN_KEY="secret-key")
    def test_outsider_cannot_obtain_a_playback_url(self):
        res = _client(self.outsider, "learner", self.outsider_profile).get(
            self.url(self.rec_open)
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(BUNNY_STREAM_TOKEN_KEY="secret-key")
    def test_wrong_batch_learner_cannot_obtain_a_playback_url(self):
        res = _client(self.parent, "learner", self.child_a).get(
            self.url(self.rec_batch_b)
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(BUNNY_STREAM_TOKEN_KEY="secret-key")
    def test_signed_url_matches_bunnys_documented_scheme(self):
        res = _client(self.parent, "learner", self.child_a).get(
            self.url(self.rec_open)
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertTrue(body["token_auth"])

        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(body["embed_url"])
        self.assertEqual(parsed.netloc, "iframe.mediadelivery.net")
        self.assertEqual(parsed.path, "/embed/4242/vid-open")
        q = parse_qs(parsed.query)
        expires = q["expires"][0]
        self.assertEqual(str(body["expires"]), expires)
        # token = SHA256_HEX(key + video_id + expires)
        self.assertEqual(
            q["token"][0],
            hashlib.sha256(f"secret-keyvid-open{expires}".encode()).hexdigest(),
        )

    @override_settings(BUNNY_STREAM_TOKEN_KEY="secret-key")
    def test_expiry_is_in_the_future_and_bounded(self):
        res = _client(self.parent, "learner", self.child_a).get(self.url(self.rec_open))
        expires = res.json()["expires"]
        now = int(timezone.now().timestamp())
        self.assertGreater(expires, now)
        self.assertLessEqual(expires - now, int(timedelta(hours=4).total_seconds()) + 5)

    @override_settings(BUNNY_STREAM_TOKEN_KEY="secret-key")
    def test_start_position_rides_alongside_but_is_not_signed(self):
        """The seek parameter is `t`, and it is outside the signature.

        This test used to assert `start=90`, which ASSERTED THE BUG: `start`
        is not a parameter Bunny recognises, so the player ignored it and
        "resume where you left off" never resumed. Bunny's documented seek
        parameter is `t`. The public API still takes `?start=` from callers —
        only the outgoing Bunny URL changed.
        """
        res = _client(self.parent, "learner", self.child_a).get(
            self.url(self.rec_open) + "?start=90"
        )
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(res.json()["embed_url"]).query)
        self.assertEqual(q["t"][0], "90")
        self.assertNotIn("start", q)
        expires = q["expires"][0]
        self.assertEqual(
            q["token"][0],
            hashlib.sha256(f"secret-keyvid-open{expires}".encode()).hexdigest(),
        )

    @override_settings(BUNNY_STREAM_TOKEN_KEY="")
    def test_unconfigured_key_reports_token_auth_false_rather_than_pretending(self):
        res = _client(self.parent, "learner", self.child_a).get(self.url(self.rec_open))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.json()["token_auth"])
        self.assertIsNone(res.json()["expires"])
        self.assertNotIn("token=", res.json()["embed_url"])

    @override_settings(BUNNY_LIBRARY_ID="", BUNNY_STREAM_TOKEN_KEY="")
    def test_no_library_configured_is_a_503_not_a_broken_url(self):
        res = _client(self.parent, "learner", self.child_a).get(self.url(self.rec_open))
        self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class BunnyUploadTicketExpiryTest(TestCase):
    """MEDIUM — one flat 1 h ticket for a transfer the form caps at 4 GB."""

    def test_small_file_keeps_the_one_hour_default(self):
        from config.bunny_signing import DEFAULT_EXPIRY_SECONDS, upload_expiry_for_size

        self.assertEqual(upload_expiry_for_size(5 * 1024 * 1024), DEFAULT_EXPIRY_SECONDS)
        self.assertEqual(upload_expiry_for_size(None), DEFAULT_EXPIRY_SECONDS)
        self.assertEqual(upload_expiry_for_size("junk"), DEFAULT_EXPIRY_SECONDS)

    def test_multi_gigabyte_file_gets_a_longer_capped_ticket(self):
        from config.bunny_signing import MAX_UPLOAD_EXPIRY_SECONDS, upload_expiry_for_size

        four_gb = 4 * 1024 * 1024 * 1024
        self.assertGreater(upload_expiry_for_size(four_gb), 3600)
        self.assertLessEqual(upload_expiry_for_size(four_gb), MAX_UPLOAD_EXPIRY_SECONDS)
        self.assertEqual(
            upload_expiry_for_size(100 * four_gb), MAX_UPLOAD_EXPIRY_SECONDS,
        )


# ---------------------------------------------------------------------------
# PHASE 1 — API hardening + delete hygiene
#
# Four findings, all pre-existing:
#   · CreateRecordingView took a client-supplied bunny_video_id.
#   · _require_recording_viewer had no is_staff branch, so the admin console
#     was denied every per-id endpoint including /playback/.
#   · _require_recording_viewer never checked is_published — latent until the
#     PATCH endpoint made unpublish a real teacher action.
#   · DeleteRecordingView's is_staff branch was unreachable (IsTeacherContext
#     rejected admins at the class gate), and deleting orphaned chapter tags.
# ---------------------------------------------------------------------------


class UnpublishedRecordingVisibilityTest(RecordingAccessBase):
    """An unpublished recording must be unreachable to a learner on EVERY
    per-id endpoint, not just absent from the list."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rec_draft = SessionRecording.objects.create(
            subject=cls.subject, batch=None, title="Not published yet",
            bunny_video_id="vid-draft", uploaded_by=cls.teacher, status=4,
            duration_seconds=600, is_published=False,
        )

    def test_entitled_learner_is_denied_every_per_id_endpoint(self):
        c = _client(self.parent, "learner", self.child_a)
        base = f"/api/courses/recordings/{self.rec_draft.id}"
        for path in (f"{base}/", f"{base}/playback/", f"{base}/progress/",
                     f"{base}/status/", f"{base}/notes/"):
            with self.subTest(path=path):
                self.assertEqual(
                    c.get(path).status_code, status.HTTP_403_FORBIDDEN,
                )

    def test_learner_cannot_write_progress_against_an_unpublished_recording(self):
        res = _client(self.parent, "learner", self.child_a).post(
            f"/api/courses/recordings/{self.rec_draft.id}/progress/save/",
            {"last_position": 30}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_assigned_teacher_still_sees_it(self):
        res = _client(self.teacher, "teacher").get(
            f"/api/courses/recordings/{self.rec_draft.id}/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_published_recording_is_unaffected(self):
        res = _client(self.parent, "learner", self.child_a).get(
            f"/api/courses/recordings/{self.rec_open.id}/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class StaffRecordingViewerTest(RecordingAccessBase):
    """An admin is not a teacher and has no learner profile. Before the
    is_staff branch they were denied with 'Select a learner profile.'"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create_user(
            username="admin1", email="admin1@example.com", password="pw",
            is_staff=True,
        )

    def test_staff_can_read_a_recording_they_have_no_profile_for(self):
        res = _client(self.admin, "admin").get(
            f"/api/courses/recordings/{self.rec_batch_b.id}/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    @override_settings(BUNNY_LIBRARY_ID="42", BUNNY_STREAM_TOKEN_KEY="k")
    def test_staff_can_get_a_playback_url(self):
        res = _client(self.admin, "admin").get(
            f"/api/courses/recordings/{self.rec_batch_b.id}/playback/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("embed_url", res.data)


class RecordingDeleteAuthorizationTest(RecordingAccessBase):
    """DeleteRecordingView's is_staff branch was unreachable dead code."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create_user(
            username="admin2", email="admin2@example.com", password="pw",
            is_staff=True,
        )
        cls.other_teacher = User.objects.create_user(
            username="teach2", email="teach2@example.com", password="pw",
        )
        UserRole.objects.create(
            user=cls.other_teacher, role=Role.objects.get(name=Role.TEACHER),
        )

    def _del_url(self, rec):
        return f"/api/courses/recordings/{rec.id}/delete/"

    @patch("courses.views_recordings.requests.delete")
    def test_staff_without_the_teacher_role_can_delete(self, _mock):
        rec = SessionRecording.objects.create(
            subject=self.subject, title="doomed", bunny_video_id="v1",
            uploaded_by=self.teacher, status=4,
        )
        res = _client(self.admin, "admin").delete(self._del_url(rec))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SessionRecording.objects.filter(id=rec.id).exists())

    @patch("courses.views_recordings.requests.delete")
    def test_the_assigned_teacher_can_delete(self, _mock):
        rec = SessionRecording.objects.create(
            subject=self.subject, title="doomed", bunny_video_id="v2",
            uploaded_by=self.teacher, status=4,
        )
        res = _client(self.teacher, "teacher").delete(self._del_url(rec))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

    def test_a_teacher_not_assigned_to_the_subject_is_denied(self):
        res = _client(self.other_teacher, "teacher").delete(
            self._del_url(self.rec_open)
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            SessionRecording.objects.filter(id=self.rec_open.id).exists()
        )

    def test_an_enrolled_learner_is_denied(self):
        res = _client(self.parent, "learner", self.child_a).delete(
            self._del_url(self.rec_open)
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            SessionRecording.objects.filter(id=self.rec_open.id).exists()
        )


class RecordingDeleteCleanupTest(RecordingAccessBase):
    """ContentChapterTag is a GENERIC relation with no FK — nothing cascades."""

    def _make(self):
        from .models import Chapter
        from .chapter_tags import set_tags

        rec = SessionRecording.objects.create(
            subject=self.subject, title="tagged", bunny_video_id="v3",
            uploaded_by=self.teacher, status=4,
        )
        ch1 = Chapter.objects.create(subject=self.subject, title="Ch 1", order=1)
        ch2 = Chapter.objects.create(subject=self.subject, title="Ch 2", order=2)
        set_tags(rec, [(ch1, "", 0), (ch2, "", 1)])
        return rec

    def _tag_count(self, rec):
        from django.contrib.contenttypes.models import ContentType
        from .models_chapter_tags import ContentChapterTag

        return ContentChapterTag.objects.filter(
            content_type=ContentType.objects.get_for_model(SessionRecording),
            object_id=rec.pk,
        ).count()

    @patch("courses.views_recordings.requests.delete")
    def test_deleting_a_recording_leaves_no_orphan_chapter_tags(self, _mock):
        rec = self._make()
        self.assertEqual(self._tag_count(rec), 2)

        res = _client(self.teacher, "teacher").delete(
            f"/api/courses/recordings/{rec.id}/delete/"
        )
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self._tag_count(rec), 0)

    @patch("courses.views_recordings.requests.delete",
           side_effect=Exception("bunny is down"))
    def test_a_failing_bunny_delete_does_not_block_the_row_delete(self, mock_del):
        rec = SessionRecording.objects.create(
            subject=self.subject, title="doomed", bunny_video_id="v4",
            uploaded_by=self.teacher, status=4,
        )
        # captureOnCommitCallbacks, or the Bunny call never runs: TestCase
        # wraps each test in a transaction that is rolled back, so on_commit
        # hooks are discarded and the assertion below would pass/fail for
        # entirely the wrong reason.
        with self.captureOnCommitCallbacks(execute=True):
            res = _client(self.teacher, "teacher").delete(
                f"/api/courses/recordings/{rec.id}/delete/"
            )
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SessionRecording.objects.filter(id=rec.id).exists())
        self.assertTrue(mock_del.called)

    @patch("courses.views_recordings.requests.delete")
    def test_bunny_is_only_told_after_the_row_is_really_gone(self, mock_del):
        """The Bunny call used to run FIRST, so a DB failure left a live row
        pointing at a video that no longer existed."""
        rec = SessionRecording.objects.create(
            subject=self.subject, title="doomed", bunny_video_id="v5",
            uploaded_by=self.teacher, status=4,
        )
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            _client(self.teacher, "teacher").delete(
                f"/api/courses/recordings/{rec.id}/delete/"
            )
            # THE ORDERING GUARANTEE: the request has fully returned and Bunny
            # has still not been told, because the callback is only queued.
            # (`callbacks` itself stays empty until the block exits — Django
            # collects it in __exit__ — so assert on the mock, not on len.)
            self.assertFalse(mock_del.called)

        self.assertEqual(len(callbacks), 1)
        self.assertTrue(mock_del.called)
        self.assertFalse(SessionRecording.objects.filter(id=rec.id).exists())

    @patch("courses.views_recordings.requests.delete")
    def test_a_recording_with_no_video_schedules_no_bunny_call(self, mock_del):
        rec = SessionRecording.objects.create(
            subject=self.subject, title="no video", bunny_video_id="",
            uploaded_by=self.teacher, status=0,
        )
        with self.captureOnCommitCallbacks(execute=True):
            res = _client(self.teacher, "teacher").delete(
                f"/api/courses/recordings/{rec.id}/delete/"
            )
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(mock_del.called)


class RemovedCreateRecordingRouteTest(RecordingAccessBase):
    def test_the_client_supplied_video_id_create_route_is_gone(self):
        res = _client(self.teacher, "teacher").post(
            f"/api/courses/subjects/{self.subject.id}/recordings/create/",
            {"title": "mine", "bunny_video_id": "someone-elses-video"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


class SessionRecordingSerializerIsReadOnlyTest(TestCase):
    """Guards the hole CreateRecordingView opened: a ModelSerializer with no
    read_only_fields let a caller write bunny_video_id/status/is_published."""

    def test_every_declared_field_is_read_only(self):
        from .serializers_recordings import SessionRecordingSerializer

        writable = [
            name for name, field in SessionRecordingSerializer().fields.items()
            if not field.read_only
        ]
        self.assertEqual(writable, [])


# ---------------------------------------------------------------------------
# PHASE 2 — trim offsets
#
# A trim is presentational: it moves where the player starts and where the
# client stops it. The cover below is about the arithmetic staying consistent
# across the serializer, both progress views and the playback URL — because
# three of them disagreeing is how a progress bar reads 143%.
# ---------------------------------------------------------------------------


class RecordingTrimWindowTest(TestCase):
    """The model properties every other layer reads."""

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(title="C")
        cls.subject = Subject.objects.create(course=cls.course, name="S")
        cls.teacher = User.objects.create_user(
            username="t-trim", email="t-trim@example.com", password="pw",
        )

    def _rec(self, **kw):
        kw.setdefault("status", 4)
        return SessionRecording.objects.create(
            subject=self.subject, title="r", bunny_video_id="v",
            uploaded_by=self.teacher, **kw,
        )

    def test_untrimmed_window_is_the_whole_video(self):
        rec = self._rec(duration_seconds=600)
        self.assertEqual(rec.effective_start_seconds, 0)
        self.assertEqual(rec.effective_end_seconds, 600)
        self.assertEqual(rec.effective_duration_seconds, 600)

    def test_trimmed_window(self):
        rec = self._rec(duration_seconds=600, trim_start_seconds=120,
                        trim_end_seconds=420)
        self.assertEqual(rec.effective_start_seconds, 120)
        self.assertEqual(rec.effective_end_seconds, 420)
        self.assertEqual(rec.effective_duration_seconds, 300)

    def test_length_unknown_while_transcoding_is_none_not_zero(self):
        """duration_seconds is NULL until Bunny finishes and something polls."""
        rec = self._rec(status=1)
        self.assertIsNone(rec.effective_end_seconds)
        self.assertIsNone(rec.effective_duration_seconds)
        self.assertIsNone(rec.percent_watched(0))

    def test_clamp_pulls_a_position_into_the_window(self):
        rec = self._rec(duration_seconds=600, trim_start_seconds=120,
                        trim_end_seconds=420)
        self.assertEqual(rec.clamp_position(5), 120.0)
        self.assertEqual(rec.clamp_position(9999), 420.0)
        self.assertEqual(rec.clamp_position(300), 300.0)
        self.assertEqual(rec.clamp_position("junk"), 120.0)

    def test_percent_is_measured_against_the_visible_window(self):
        rec = self._rec(duration_seconds=600, trim_start_seconds=120,
                        trim_end_seconds=420)
        # Halfway through the 300s window, not through the 600s video.
        self.assertEqual(rec.percent_watched(270), 50.0)

    def test_a_trim_applied_after_watching_reads_100_not_143(self):
        rec = self._rec(duration_seconds=600, trim_start_seconds=120,
                        trim_end_seconds=420)
        self.assertEqual(rec.percent_watched(550), 100.0)

    def test_inverted_trim_window_is_rejected_by_the_database(self):
        from django.db import IntegrityError, transaction as db_transaction

        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                self._rec(duration_seconds=600, trim_start_seconds=300,
                          trim_end_seconds=200)


class RecordingTrimProgressTest(RecordingAccessBase):
    """last_position stays in RAW player seconds; the window applies on read."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rec_trimmed = SessionRecording.objects.create(
            subject=cls.subject, batch=None, title="Trimmed",
            bunny_video_id="vid-trim", uploaded_by=cls.teacher, status=4,
            duration_seconds=600, is_published=True,
            trim_start_seconds=120, trim_end_seconds=420,
        )

    def _save(self, pos):
        return _client(self.parent, "learner", self.child_a).post(
            f"/api/courses/recordings/{self.rec_trimmed.id}/progress/save/",
            {"last_position": pos}, format="json",
        )

    def _read(self):
        return _client(self.parent, "learner", self.child_a).get(
            f"/api/courses/recordings/{self.rec_trimmed.id}/progress/"
        ).json()

    def test_midpoint_of_the_window_reads_fifty_percent(self):
        self._save(270)
        self.assertEqual(self._read()["percent_complete"], 50.0)

    def test_a_position_past_the_trim_end_is_clamped_when_stored(self):
        self._save(9999)
        self.assertEqual(
            VideoProgress.objects.get(recording=self.rec_trimmed).last_position,
            420.0,
        )

    def test_auto_complete_fires_at_the_window_end_not_the_video_end(self):
        """Against raw duration this recording could never be completed — the
        last 5 minutes needed to reach it are the ones the trim removes."""
        self._save(411)
        self.assertTrue(
            VideoProgress.objects.get(recording=self.rec_trimmed).completed
        )

    def test_a_fresh_viewer_starts_at_the_trim_point(self):
        self.assertEqual(self._read()["last_position"], 120)

    def test_the_response_carries_the_resolved_window(self):
        body = self._read()
        self.assertEqual(body["trim_start_seconds"], 120)
        self.assertEqual(body["trim_end_seconds"], 420)
        self.assertEqual(body["effective_duration_seconds"], 300)
        # duration_seconds still means the FULL Bunny length.
        self.assertEqual(body["duration_seconds"], 600)

    def test_an_untrimmed_recording_is_unchanged(self):
        c = _client(self.parent, "learner", self.child_a)
        c.post(
            f"/api/courses/recordings/{self.rec_open.id}/progress/save/",
            {"last_position": 300}, format="json",
        )
        body = c.get(
            f"/api/courses/recordings/{self.rec_open.id}/progress/"
        ).json()
        self.assertEqual(body["percent_complete"], 50.0)
        self.assertIsNone(body["trim_start_seconds"])
        self.assertEqual(body["effective_duration_seconds"], 600)


class RecordingTrimPlaybackTest(RecordingAccessBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rec_trimmed = SessionRecording.objects.create(
            subject=cls.subject, batch=None, title="Trimmed",
            bunny_video_id="vid-trim", uploaded_by=cls.teacher, status=4,
            duration_seconds=600, is_published=True,
            trim_start_seconds=120, trim_end_seconds=420,
        )

    def _play(self, qs=""):
        return _client(self.parent, "learner", self.child_a).get(
            f"/api/courses/recordings/{self.rec_trimmed.id}/playback/{qs}"
        ).json()

    @override_settings(BUNNY_LIBRARY_ID="42", BUNNY_STREAM_TOKEN_KEY="k")
    def test_playback_defaults_to_the_trim_point(self):
        from urllib.parse import parse_qs, urlparse

        body = self._play()
        self.assertEqual(body["start"], 120)
        q = parse_qs(urlparse(body["embed_url"]).query)
        self.assertEqual(q["t"][0], "120")

    @override_settings(BUNNY_LIBRARY_ID="42", BUNNY_STREAM_TOKEN_KEY="k")
    def test_a_start_before_the_trim_is_pulled_forward(self):
        self.assertEqual(self._play("?start=5")["start"], 120)

    @override_settings(BUNNY_LIBRARY_ID="42", BUNNY_STREAM_TOKEN_KEY="k")
    def test_a_start_past_the_trim_end_is_pulled_back(self):
        self.assertEqual(self._play("?start=9999")["start"], 420)

    @override_settings(BUNNY_LIBRARY_ID="42", BUNNY_STREAM_TOKEN_KEY="k")
    def test_the_window_is_reported_so_clients_do_not_recompute_it(self):
        body = self._play()
        self.assertEqual(body["trim_start_seconds"], 120)
        self.assertEqual(body["trim_end_seconds"], 420)
        self.assertEqual(body["effective_duration_seconds"], 300)
        self.assertEqual(body["duration_seconds"], 600)


# ---------------------------------------------------------------------------
# PHASE 3 — the PATCH endpoint
#
# There was no update endpoint of any kind before this. The tests that matter
# most are the whitelist ones: they are the proof that adding an edit path did
# not reintroduce CreateRecordingView's client-supplied-bunny_video_id hole.
# ---------------------------------------------------------------------------


class RecordingUpdateBase(RecordingAccessBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create_user(
            username="admin-upd", email="admin-upd@example.com", password="pw",
            is_staff=True,
        )
        cls.other_teacher = User.objects.create_user(
            username="teach-upd", email="teach-upd@example.com", password="pw",
        )
        UserRole.objects.create(
            user=cls.other_teacher, role=Role.objects.get(name=Role.TEACHER),
        )

    def setUp(self):
        self.rec = SessionRecording.objects.create(
            subject=self.subject, batch=None, title="Original title",
            description="original", bunny_video_id="vid-edit",
            uploaded_by=self.teacher, status=4, duration_seconds=600,
            is_published=True,
        )

    def url(self, rec=None):
        return f"/api/courses/recordings/{(rec or self.rec).id}/"

    def patch(self, payload, user=None, context="teacher", profile=None):
        return _client(user or self.teacher, context, profile).patch(
            self.url(), payload, format="json",
        )


class RecordingUpdateGateTest(RecordingUpdateBase):
    def test_the_assigned_teacher_can_edit(self):
        res = self.patch({"title": "Corrected title"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.title, "Corrected title")

    def test_staff_can_edit(self):
        res = self.patch({"title": "Admin fixed it"}, user=self.admin,
                         context="admin")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_a_teacher_not_assigned_to_the_subject_is_denied(self):
        res = self.patch({"title": "hijack"}, user=self.other_teacher)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.title, "Original title")

    def test_an_enrolled_learner_is_denied(self):
        res = self.patch({"title": "hijack"}, user=self.parent,
                         context="learner", profile=self.child_a)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_outsider_is_denied(self):
        res = self.patch({"title": "hijack"}, user=self.outsider,
                         context="learner", profile=self.outsider_profile)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_put_is_not_offered(self):
        res = _client(self.teacher, "teacher").put(
            self.url(), {"title": "x"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class RecordingUpdateFieldWhitelistTest(RecordingUpdateBase):
    """The actual proof that the whitelist is a boundary, not decoration."""

    def test_protected_fields_are_ignored_not_written(self):
        res = self.patch({
            "title": "Legit change",
            "bunny_video_id": "someone-elses-video",
            "status": 0,
            "duration_seconds": 99999,
            "thumbnail_url": "https://evil.example.com/x.jpg",
            "subject": str(self.other_subject.id),
            "uploaded_by": self.other_teacher.id,
            "id": "00000000-0000-0000-0000-000000000000",
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.rec.refresh_from_db()
        self.assertEqual(self.rec.title, "Legit change")
        self.assertEqual(self.rec.bunny_video_id, "vid-edit")
        self.assertEqual(self.rec.status, 4)
        self.assertEqual(self.rec.duration_seconds, 600)
        self.assertEqual(self.rec.thumbnail_url, "")
        self.assertEqual(self.rec.subject_id, self.subject.id)
        self.assertEqual(self.rec.uploaded_by_id, self.teacher.id)

    def test_a_blank_title_is_rejected(self):
        res = self.patch({"title": "   "})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_description_and_session_date_are_editable(self):
        res = self.patch({"description": "new notes", "session_date": "2026-08-01"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.description, "new notes")
        self.assertEqual(str(self.rec.session_date), "2026-08-01")


class RecordingUpdateValidationTest(RecordingUpdateBase):
    def test_a_batch_from_another_course_is_rejected(self):
        res = self.patch({"batch_id": str(self.foreign_batch.id)})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("batch_id", res.json())
        self.rec.refresh_from_db()
        self.assertIsNone(self.rec.batch_id)

    def test_a_batch_from_this_course_is_accepted(self):
        res = self.patch({"batch_id": str(self.batch_a.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.batch_id, self.batch_a.id)

    def test_batch_can_be_cleared_back_to_course_wide(self):
        self.rec.batch = self.batch_a
        self.rec.save()
        res = self.patch({"batch_id": None})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.rec.refresh_from_db()
        self.assertIsNone(self.rec.batch_id)

    def test_a_chapter_from_another_subject_is_rejected(self):
        from .models import Chapter
        foreign = Chapter.objects.create(
            subject=self.other_subject, title="Foreign ch", order=1,
        )
        res = self.patch({"chapter_id": str(foreign.id)})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("chapter_id", res.json())

    def test_an_inverted_trim_window_is_a_400_not_a_500(self):
        res = self.patch({"trim_start_seconds": 300, "trim_end_seconds": 200})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_inverted_window_across_two_patches_is_still_a_400(self):
        """The check must run against the MERGED state, or a partial update
        slips an inverted window past the serializer into the DB constraint."""
        self.patch({"trim_start_seconds": 300, "trim_end_seconds": 400})
        res = self.patch({"trim_end_seconds": 200})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_trim_past_the_end_of_the_video_is_rejected(self):
        res = self.patch({"trim_end_seconds": 9999})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_trim_on_a_still_transcoding_upload_is_allowed(self):
        """duration_seconds is NULL until Bunny finishes, so the range check
        cannot run — blocking the edit would be worse than accepting it."""
        rec = SessionRecording.objects.create(
            subject=self.subject, title="processing", bunny_video_id="vp",
            uploaded_by=self.teacher, status=1,
        )
        res = _client(self.teacher, "teacher").patch(
            self.url(rec), {"trim_start_seconds": 30}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_a_trim_can_be_cleared(self):
        self.patch({"trim_start_seconds": 60, "trim_end_seconds": 500})
        res = self.patch({"trim_start_seconds": None, "trim_end_seconds": None})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.rec.refresh_from_db()
        self.assertIsNone(self.rec.trim_start_seconds)
        self.assertIsNone(self.rec.trim_end_seconds)

    def test_a_valid_trim_round_trips_into_the_response(self):
        res = self.patch({"trim_start_seconds": 120, "trim_end_seconds": 420})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["effective_duration_seconds"], 300)


class RecordingUpdateChapterTagTest(RecordingUpdateBase):
    def test_tags_are_written_and_backfill_the_legacy_chapter_fk(self):
        from .models import Chapter
        ch = Chapter.objects.create(subject=self.subject, title="Ch 1", order=1)

        res = self.patch({"chapter_tags": [{"chapter_id": str(ch.id)}]})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.rec.refresh_from_db()
        # The additive invariant — see chapter_tags.primary_chapter().
        self.assertEqual(self.rec.chapter_id, ch.id)
        self.assertEqual(len(res.json()["chapter_tags"]), 1)

    def test_a_title_only_patch_leaves_existing_tags_alone(self):
        from .models import Chapter
        ch = Chapter.objects.create(subject=self.subject, title="Ch 1", order=1)
        self.patch({"chapter_tags": [{"chapter_id": str(ch.id)}]})

        res = self.patch({"title": "Just the title"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.json()["chapter_tags"]), 1)


class RecordingPublishNotificationTest(RecordingUpdateBase):
    """Only the False->True edge notifies."""

    def setUp(self):
        super().setUp()
        self.rec.is_published = False
        self.rec.batch = self.batch_a
        self.rec.save()

    def _activity_count(self):
        from activity.models import Activity
        return Activity.objects.filter(
            title__startswith="New recording:",
        ).count()

    def test_publishing_notifies_the_batch_that_can_see_it(self):
        before = self._activity_count()
        res = self.patch({"is_published": True})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Both children are enrolled in Batch A.
        self.assertEqual(self._activity_count() - before, 2)

    def test_editing_an_already_published_recording_is_silent(self):
        self.patch({"is_published": True})
        before = self._activity_count()
        self.patch({"title": "typo fixed"})
        self.assertEqual(self._activity_count(), before)

    def test_unpublishing_notifies_nobody(self):
        self.patch({"is_published": True})
        before = self._activity_count()
        self.patch({"is_published": False})
        self.assertEqual(self._activity_count(), before)


class RecordingLiveSessionBatchOverrideTest(RecordingAccessBase):
    """Uploading from a Live Session must not override an explicit
    'All batches' choice with that session's own batch.

    The old code was `if batch_id: ... elif live_session: batch =
    live_session.batch`, which cannot distinguish "the teacher deliberately
    chose All batches" (batch_id=null) from "the field was never sent". A
    recording meant for the whole course silently reached one batch, so half
    the students never saw it and nothing reported a problem. No test covered
    this combination; reproduced live against a running server before fixing.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from livestream.models import LiveSession

        now = timezone.now()
        cls.session = LiveSession.objects.create(
            course=cls.course, subject=cls.subject, batch=cls.batch_a,
            title="Batched class", room_name="session_override_test",
            start_time=now - timedelta(days=1),
            end_time=now - timedelta(days=1) + timedelta(hours=1),
            created_by=cls.teacher,
        )

    def _save(self, payload):
        payload.setdefault("title", "From a live session")
        payload.setdefault("video_id", "vid-override")
        return _client(self.teacher, "teacher").post(
            f"/api/courses/subjects/{self.subject.id}/recordings/save/",
            payload, format="json",
        )

    def test_an_explicit_null_batch_stays_course_wide(self):
        res = self._save({"live_session_id": str(self.session.id),
                          "batch_id": None})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(
            SessionRecording.objects.get(id=res.json()["id"]).batch_id,
            "an explicit All-batches choice was overridden by the session's batch",
        )

    def test_an_omitted_batch_still_inherits_the_sessions_batch(self):
        """The convenience the inheritance exists for must keep working."""
        res = self._save({"live_session_id": str(self.session.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            SessionRecording.objects.get(id=res.json()["id"]).batch_id,
            self.batch_a.id,
        )

    def test_an_explicit_batch_still_wins(self):
        res = self._save({"live_session_id": str(self.session.id),
                          "batch_id": str(self.batch_b.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            SessionRecording.objects.get(id=res.json()["id"]).batch_id,
            self.batch_b.id,
        )

    def test_no_live_session_and_no_batch_is_course_wide(self):
        res = self._save({})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(
            SessionRecording.objects.get(id=res.json()["id"]).batch_id
        )


class SaveRecordingChapterTagTest(RecordingAccessBase):
    """The upload endpoint ignored every chapter key it was sent.

    `SessionRecording` has carried `chapter`, `chapter_tags`, `chapter_note`
    and `no_specific_chapter` since the tagging system landed and the read
    serializer returns all four — but SaveRecordingView read none of them, so
    a teacher who tagged a recording on the upload form watched the tags
    vanish on save with no error. Same contract, and the same four wire keys,
    as the material upload and SessionRecordingUpdateSerializer.
    """

    def save_url(self):
        return f"/api/courses/subjects/{self.subject.id}/recordings/save/"

    def _save(self, body):
        return _client(self.teacher, "teacher").post(
            self.save_url(),
            {"title": "Lecture", "video_id": "vid-tag", **body},
            format="json",
        )

    def _chapter(self, title="Ch 1", **kwargs):
        from .models import Chapter
        return Chapter.objects.create(
            subject=self.subject, title=title, order=1, **kwargs,
        )

    def test_a_syllabus_chapter_is_tagged_and_backfills_the_legacy_fk(self):
        ch = self._chapter()
        res = self._save({"chapter_tags": [{"chapter_id": str(ch.id)}]})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        rec = SessionRecording.objects.get(id=res.json()["id"])
        # The additive invariant — see chapter_tags.primary_chapter().
        self.assertEqual(rec.chapter_id, ch.id)
        self.assertEqual(len(res.json()["chapter_tags"]), 1)
        self.assertEqual(res.json()["chapter_tags"][0]["chapter_id"], str(ch.id))

    def test_a_custom_label_stays_private_by_default(self):
        """No `save_chapters_to_course` → free text, and the shared syllabus
        is left exactly as it was. This is what the legacy `custom_chapter`
        key on the material upload got wrong."""
        from .models import Chapter

        res = self._save({"chapter_tags": [{"label": "Board-pattern numericals"}]})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        tags = res.json()["chapter_tags"]
        self.assertEqual(len(tags), 1)
        self.assertIsNone(tags[0]["chapter_id"])
        self.assertEqual(tags[0]["label"], "Board-pattern numericals")
        self.assertFalse(Chapter.objects.filter(subject=self.subject).exists())

        rec = SessionRecording.objects.get(id=res.json()["id"])
        self.assertIsNone(rec.chapter_id)

    def test_save_chapters_to_course_promotes_a_label(self):
        from .models import Chapter

        res = self._save({
            "chapter_tags": [{"label": "Rotational motion"}],
            "save_chapters_to_course": True,
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        created = Chapter.objects.get(subject=self.subject, title="Rotational motion")
        self.assertTrue(created.is_custom)
        self.assertEqual(created.created_by_id, self.teacher.id)
        rec = SessionRecording.objects.get(id=res.json()["id"])
        self.assertEqual(rec.chapter_id, created.id)

    def test_a_chapter_and_a_label_can_be_combined(self):
        ch = self._chapter()
        res = self._save({"chapter_tags": [
            {"chapter_id": str(ch.id)},
            {"label": "plus a worked example"},
        ]})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.json()["chapter_tags"]), 2)
        # First resolved CHAPTER, not first tag.
        self.assertEqual(
            SessionRecording.objects.get(id=res.json()["id"]).chapter_id, ch.id,
        )

    def test_no_specific_chapter_is_a_real_saved_state(self):
        res = self._save({"no_specific_chapter": True, "chapter_note": "Revision"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        rec = SessionRecording.objects.get(id=res.json()["id"])
        self.assertTrue(rec.no_specific_chapter)
        self.assertEqual(rec.chapter_note, "Revision")
        self.assertIsNone(rec.chapter_id)
        self.assertEqual(res.json()["chapter_tags"], [])

    def test_no_specific_chapter_false_is_not_read_as_true(self):
        """`_as_bool` also accepts the multipart string dialect, where
        "false" is a truthy Python string."""
        res = self._save({"no_specific_chapter": "false",
                          "chapter_tags": [{"label": "Something"}]})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(
            SessionRecording.objects.get(id=res.json()["id"]).no_specific_chapter
        )

    def test_tags_plus_no_specific_chapter_is_rejected(self):
        ch = self._chapter()
        res = self._save({
            "no_specific_chapter": True,
            "chapter_tags": [{"chapter_id": str(ch.id)}],
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SessionRecording.objects.filter(title="Lecture").exists())

    def test_a_chapter_from_another_subject_is_rejected(self):
        from .models import Chapter
        foreign = Chapter.objects.create(
            subject=self.other_subject, title="Not mine", order=1,
        )
        res = self._save({"chapter_tags": [{"chapter_id": str(foreign.id)}]})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SessionRecording.objects.filter(title="Lecture").exists())

    def test_an_untagged_upload_still_saves(self):
        """Every existing caller sends none of these keys."""
        res = self._save({})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        rec = SessionRecording.objects.get(id=res.json()["id"])
        self.assertIsNone(rec.chapter_id)
        self.assertFalse(rec.no_specific_chapter)
        self.assertEqual(rec.chapter_note, "")
        self.assertEqual(res.json()["chapter_tags"], [])

    def test_a_foreign_batch_404_leaves_no_stray_chapter_behind(self):
        """Ordering guard: tags resolve AFTER the batch lookup, so a rejected
        request cannot first mint a Chapter row via save_chapters_to_course."""
        from .models import Chapter

        res = self._save({
            "batch_id": str(self.foreign_batch.id),
            "chapter_tags": [{"label": "Ghost chapter"}],
            "save_chapters_to_course": True,
        })
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Chapter.objects.filter(title="Ghost chapter").exists())
