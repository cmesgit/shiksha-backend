"""Starting LiveKit Egress on a teacher's join — phase 1 of auto recording.

The outbound LiveKit call is patched at exactly one seam
(`_start_room_composite`), so these tests exercise the real request builder,
the real claim/idempotence logic and the real webhook handler. What they
cannot prove is that Bunny accepts the credentials — that needs a real
storage zone, and `RequestShapeTest` below pins every field that would make
it fail instead.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream import views as lv
from livestream.models import LiveSession, LiveSessionEgress
from livestream.services import egress as egress_svc
from livestream.services.token import build_identity

User = get_user_model()

# A configured-and-switched-on deployment. Applied per-test rather than
# globally so the default (off) path is exercised too.
EGRESS_ON = dict(
    LIVEKIT_EGRESS_ENABLED=True,
    LIVEKIT_URL="wss://example.livekit.cloud",
    LIVEKIT_API_KEY="lk-key",
    LIVEKIT_API_SECRET="lk-secret",
    BUNNY_EGRESS_ZONE="shiksha-class-egress",
    BUNNY_EGRESS_API_KEY="zone-password",
    BUNNY_EGRESS_REGION="de",
    BUNNY_EGRESS_S3_HOST="de-s3.storage.bunnycdn.com",
    BUNNY_EGRESS_PREFIX="class-egress",
)


def _fake_info(egress_id="EG_test", status=None):
    """Stand-in for livekit EgressInfo. `status` as the protobuf int LiveKit
    really sends (EgressStatus starts at EGRESS_STARTING=0, so 1 is
    EGRESS_ACTIVE), not the string name we store."""
    return SimpleNamespace(egress_id=egress_id, status=status)


class EgressStartBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="eg_t@x.com", email="eg_t@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=self.teacher, role=role, is_active=True,
                                is_primary=True)
        board = Board.objects.create(name="EGBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="EG10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Maths")
        self.batch = Batch.objects.create(course=self.course, name="EG-A")
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=self.teacher, batch=None,
            is_active=True)

        self.student = User.objects.create_user(
            username="eg_s@x.com", email="eg_s@x.com", password="x")
        srole, _ = Role.objects.get_or_create(name="STUDENT")
        UserRole.objects.create(user=self.student, role=srole, is_active=True)
        self.profile = LearnerProfile.objects.create(
            account=self.student, display_name="S", full_name="S",
            student_id="EG1", is_default=True)
        Enrollment.objects.create(
            user=self.student, learner_profile=self.profile,
            course=self.course, batch=self.batch,
            status=Enrollment.STATUS_ACTIVE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Maths", start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1), room_name="room_eg",
            created_by=self.teacher, status=LiveSession.STATUS_WAITING,
        )

    def teacher_event(self):
        return SimpleNamespace(
            room=SimpleNamespace(name="room_eg"),
            participant=SimpleNamespace(
                identity=build_identity(self.teacher.id, self.session.id)),
        )

    def student_event(self):
        return SimpleNamespace(
            room=SimpleNamespace(name="room_eg"),
            participant=SimpleNamespace(
                identity=build_identity(
                self.student.id, self.session.id, self.profile.id)),
        )


class EgressDisabledTest(EgressStartBase):
    """The default everywhere today: egress unconfigured. Nothing may change."""

    def test_no_call_and_no_row_when_disabled(self):
        with patch.object(egress_svc, "_start_room_composite") as call:
            result = egress_svc.start_session_egress(self.session)
        self.assertIsNone(result)
        self.assertFalse(call.called)
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_teacher_join_is_unaffected_when_disabled(self):
        """The whole point of the kill switch: the class still goes LIVE."""
        with patch.object(egress_svc, "_start_room_composite") as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.teacher_event())
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_LIVE)
        self.assertFalse(call.called)
        self.assertEqual(LiveSessionEgress.objects.count(), 0)


@override_settings(**EGRESS_ON)
class EgressStartTest(EgressStartBase):

    def test_records_egress_id_and_status_from_livekit(self):
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_abc", status=1),
        ) as call:
            row = egress_svc.start_session_egress(self.session)
        self.assertTrue(call.called)
        row.refresh_from_db()
        self.assertEqual(row.egress_id, "EG_abc")
        self.assertEqual(row.status, LiveSessionEgress.STATUS_ACTIVE)
        self.assertIsNotNone(row.started_at)
        self.assertTrue(row.storage_key.endswith(".mp4"))

    def test_unknown_status_falls_back_instead_of_raising(self):
        """An SDK that adds an EgressStatus must not break a live class."""
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_new", status=9999),
        ):
            row = egress_svc.start_session_egress(self.session)
        self.assertEqual(row.status, LiveSessionEgress.STATUS_STARTING)

    def test_start_failure_is_recorded_not_raised(self):
        """A recording that cannot start must never take the class down."""
        with patch.object(
            egress_svc, "_start_room_composite",
            side_effect=RuntimeError("livekit unreachable"),
        ):
            row = egress_svc.start_session_egress(self.session)
        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_START_FAILED)
        self.assertIn("livekit unreachable", row.error)
        self.assertEqual(row.egress_id, "")
        self.assertTrue(row.is_terminal)

    def test_session_with_no_room_name_is_skipped(self):
        self.session.room_name = ""
        self.session.save(update_fields=["room_name"])
        with patch.object(egress_svc, "_start_room_composite") as call:
            self.assertIsNone(egress_svc.start_session_egress(self.session))
        self.assertFalse(call.called)


@override_settings(**EGRESS_ON)
class EgressIdempotenceTest(EgressStartBase):
    """A teacher's client reconnecting produces a second participant_joined.
    Each extra egress is separately billed, so this is a cost bug, not just
    a duplicate row."""

    def test_second_start_while_active_does_not_call_livekit_again(self):
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_first", status=1),
        ) as call:
            first = egress_svc.start_session_egress(self.session)
            second = egress_svc.start_session_egress(self.session)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LiveSessionEgress.objects.count(), 1)

    def test_a_terminal_attempt_does_not_block_a_retry(self):
        """A failed or finished attempt must not leave the session
        permanently unrecordable — the row-per-attempt design exists for
        exactly this."""
        with patch.object(
            egress_svc, "_start_room_composite",
            side_effect=RuntimeError("transient"),
        ):
            failed = egress_svc.start_session_egress(self.session)
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_retry", status=1),
        ) as call:
            retried = egress_svc.start_session_egress(self.session)
        self.assertTrue(call.called)
        self.assertNotEqual(failed.pk, retried.pk)
        self.assertEqual(LiveSessionEgress.objects.count(), 2)


@override_settings(**EGRESS_ON)
class EgressTriggerTest(EgressStartBase):
    """Where the start hangs off. Getting this wrong bills empty rooms."""

    def test_teacher_join_starts_recording(self):
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_join", status=1),
        ) as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.teacher_event())
        self.assertTrue(call.called)
        self.assertEqual(
            LiveSessionEgress.objects.get().egress_id, "EG_join",
        )

    def test_student_join_does_not_start_recording(self):
        """The reason this is not on room_started: a student arriving first
        must not begin a billed recording of an empty classroom."""
        with patch.object(egress_svc, "_start_room_composite") as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.student_event())
        self.assertFalse(call.called)
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_room_started_does_not_start_recording(self):
        with patch.object(egress_svc, "_start_room_composite") as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_room_started(self.teacher_event())
        self.assertFalse(call.called)
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_teacher_rejoin_does_not_start_a_second_recording(self):
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=_fake_info("EG_once", status=1),
        ) as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.teacher_event())
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.teacher_event())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(LiveSessionEgress.objects.count(), 1)

    def test_a_failing_egress_still_marks_the_class_live(self):
        """The kill-switch property, but for a runtime failure rather than
        configuration: students must still be told the class started."""
        with patch.object(
            egress_svc, "_start_room_composite",
            side_effect=RuntimeError("boom"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(self.teacher_event())
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_LIVE)
        self.assertEqual(
            LiveSessionEgress.objects.get().status,
            LiveSessionEgress.STATUS_START_FAILED,
        )


@override_settings(**EGRESS_ON)
class RequestShapeTest(EgressStartBase):
    """The S3 parameters. No test can prove Bunny accepts these without a
    real zone, so each field that would cause an opaque SigV4 or 404 failure
    is pinned individually instead."""

    def request(self):
        return egress_svc.build_request(self.session, "class-egress/x/y.mp4")

    def test_targets_the_sessions_own_room(self):
        self.assertEqual(self.request().room_name, "room_eg")

    def test_writes_one_mp4_with_no_sidecar_manifest(self):
        from livekit.api import EncodedFileType

        req = self.request()
        self.assertEqual(len(req.file_outputs), 1)
        out = req.file_outputs[0]
        self.assertEqual(out.file_type, EncodedFileType.MP4)
        self.assertEqual(out.filepath, "class-egress/x/y.mp4")
        self.assertTrue(out.disable_manifest)

    def test_s3_credentials_are_the_bunny_zone_not_the_cms_bucket(self):
        s3 = self.request().file_outputs[0].s3
        self.assertEqual(s3.access_key, "shiksha-class-egress")
        self.assertEqual(s3.bucket, "shiksha-class-egress")
        self.assertEqual(s3.secret, "zone-password")
        self.assertEqual(s3.region, "de")

    def test_endpoint_is_bunnys_s3_api_and_path_style(self):
        """Two failure modes pinned at once: the native Edge Storage host
        would not authenticate, and virtual-hosted style would resolve to a
        hostname Bunny does not serve."""
        s3 = self.request().file_outputs[0].s3
        self.assertEqual(s3.endpoint, "https://de-s3.storage.bunnycdn.com")
        self.assertTrue(s3.force_path_style)


@override_settings(**EGRESS_ON)
class StorageKeyTest(EgressStartBase):

    def test_key_is_prefixed_scoped_to_the_session_and_random(self):
        a = egress_svc.storage_key_for(self.session)
        b = egress_svc.storage_key_for(self.session)
        for key in (a, b):
            self.assertTrue(key.startswith(f"class-egress/{self.session.id}/"))
            self.assertTrue(key.endswith(".mp4"))
        self.assertNotEqual(
            a, b, "the random segment is a security property, not decoration",
        )

    @override_settings(BUNNY_EGRESS_PREFIX="")
    def test_empty_prefix_does_not_produce_a_leading_slash(self):
        key = egress_svc.storage_key_for(self.session)
        self.assertFalse(key.startswith("/"))
        self.assertTrue(key.startswith(f"{self.session.id}/"))
