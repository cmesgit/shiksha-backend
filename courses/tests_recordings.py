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
        res = _client(self.parent, "learner", self.child_a).get(
            self.url(self.rec_open) + "?start=90"
        )
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(res.json()["embed_url"]).query)
        self.assertEqual(q["start"][0], "90")
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


class UnownedRecordingTest(RecordingAccessBase):
    """SessionRecording.uploaded_by is nullable — phase 0 of automatic class
    recording (LiveKit Egress → Bunny).

    An egress-produced recording has no human uploader, so the column had to
    stop being non-null CASCADE. These tests pin the readers that would have
    broken, because none of them are exercised by any other test with a NULL
    uploader: each one looked None-safe on inspection, which is not the same
    as being known to be.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rec_auto = SessionRecording.objects.create(
            subject=cls.subject, batch=None, title="Auto-recorded class",
            bunny_video_id="vid-auto", uploaded_by=None, status=4,
            duration_seconds=1800, is_published=True,
        )

    def test_recording_can_exist_with_no_uploader(self):
        self.rec_auto.refresh_from_db()
        self.assertIsNone(self.rec_auto.uploaded_by_id)

    def test_serializer_reports_no_uploader_name_instead_of_raising(self):
        """get_uploaded_by_name has to survive a NULL FK — this is the field
        the student and teacher recording cards both render."""
        res = _client(self.teacher, "teacher").get(self.list_url())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        row = next(
            r for r in res.data if str(r["id"]) == str(self.rec_auto.id)
        )
        self.assertIsNone(row["uploaded_by_name"])

    def test_student_can_watch_an_unowned_recording(self):
        """The learner read path filters on batch/publication, never on
        uploader, so a recording nobody uploaded must still be visible."""
        res = _client(self.parent, "learner", self.child_a).get(
            self.list_url()
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn(
            str(self.rec_auto.id), [str(r["id"]) for r in res.data],
        )

    def test_subject_staff_can_still_delete_an_unowned_recording(self):
        """DeleteRecordingView authorizes on teaches_subject(), not on
        uploader identity. If it had used uploaded_by, an auto-recording
        would have been undeletable by anyone but a superuser."""
        with patch("courses.views_recordings.requests.delete") as mock_del:
            mock_del.return_value.status_code = 200
            res = _client(self.teacher, "teacher").delete(
                f"/api/courses/recordings/{self.rec_auto.id}/delete/"
            )
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            SessionRecording.objects.filter(id=self.rec_auto.id).exists()
        )

    def test_deleting_the_uploader_account_keeps_the_recording(self):
        """SET_NULL, not CASCADE: a teacher leaving the institution must not
        delete every class recording they ever made."""
        rec_id = self.rec_open.id
        self.assertIsNotNone(self.rec_open.uploaded_by_id)
        User.objects.filter(id=self.teacher.id).delete()
        surviving = SessionRecording.objects.filter(id=rec_id).first()
        self.assertIsNotNone(surviving)
        self.assertIsNone(surviving.uploaded_by_id)
