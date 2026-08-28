"""Who gets recorded — phase 5 of automatic class recording.

`LIVEKIT_EGRESS_ENABLED` is all-or-nothing, and egress is billed by the
minute: switching it on without a policy layer means paying to record every
class in the catalogue. These tests pin the three-level resolution and, more
importantly, that it fails CLOSED — an unreadable settings row must mean "no
recording", never "record everything".
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from courses.models import Board, Course, Subject
from global_settings.models import GlobalSettings
from livestream.models import LiveSession, LiveSessionEgress
from livestream.services import egress as egress_svc
from livestream.services.token import build_identity

User = get_user_model()

EGRESS_ON = dict(
    LIVEKIT_EGRESS_ENABLED=True,
    LIVEKIT_URL="wss://example.livekit.cloud",
    LIVEKIT_API_KEY="k",
    LIVEKIT_API_SECRET="s",
    BUNNY_EGRESS_ZONE="shiksha-class-egress",
    BUNNY_EGRESS_API_KEY="pw",
    BUNNY_EGRESS_REGION="sg",
    BUNNY_EGRESS_PREFIX="class-egress",
)


class PolicyBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="pol_t@x.com", email="pol_t@x.com", password="x")
        board = Board.objects.create(name="PolBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="Pol10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Maths")
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Algebra",
            start_time=now, end_time=now + timedelta(hours=1),
            room_name="room_pol", created_by=self.teacher,
        )

    def set_global(self, value):
        gs = GlobalSettings.load()
        gs.auto_record_classes = value
        gs.save(update_fields=["auto_record_classes"])

    def set_course(self, value):
        self.course.auto_record_enabled = value
        self.course.save(update_fields=["auto_record_enabled"])
        self.session.refresh_from_db()


class InfrastructureGateTest(PolicyBase):
    """Level 1. Nothing below it can switch recording on."""

    def test_disabled_infrastructure_beats_every_admin_toggle(self):
        self.set_global(True)
        self.set_course(True)
        self.assertFalse(egress_svc.is_recording_enabled_for(self.session))


@override_settings(**EGRESS_ON)
class GlobalDefaultTest(PolicyBase):
    """Level 3. Applies to every course that has not been decided about."""

    def test_off_by_default(self):
        """Egress is billed per minute; recording must be knowingly enabled,
        not switched on for the whole catalogue by a deploy."""
        self.assertFalse(GlobalSettings.load().auto_record_classes)
        self.assertFalse(egress_svc.is_recording_enabled_for(self.session))

    def test_global_on_records_courses_with_no_override(self):
        self.set_global(True)
        self.assertIsNone(self.course.auto_record_enabled)
        self.assertTrue(egress_svc.is_recording_enabled_for(self.session))

    def test_global_toggle_reaches_undecided_courses(self):
        """Why the course field is NULLABLE: a non-null default would have
        frozen every existing course at the migration's value, so the master
        switch would reach nothing."""
        self.set_global(True)
        self.assertTrue(egress_svc.is_recording_enabled_for(self.session))
        self.set_global(False)
        self.assertFalse(egress_svc.is_recording_enabled_for(self.session))


@override_settings(**EGRESS_ON)
class CourseOverrideTest(PolicyBase):
    """Level 2. The point of the phase: pilot on one course, not all of them."""

    def test_course_on_overrides_global_off(self):
        self.set_global(False)
        self.set_course(True)
        self.assertTrue(egress_svc.is_recording_enabled_for(self.session))

    def test_course_off_overrides_global_on(self):
        self.set_global(True)
        self.set_course(False)
        self.assertFalse(egress_svc.is_recording_enabled_for(self.session))

    def test_null_is_not_false(self):
        """NULL means "no decision", so it must defer rather than block."""
        self.set_global(True)
        self.set_course(None)
        self.assertTrue(egress_svc.is_recording_enabled_for(self.session))

    def test_one_course_can_be_piloted_without_billing_the_rest(self):
        self.set_global(False)
        self.set_course(True)
        other_course = Course.objects.create(
            board=self.course.board, title="Other10", class_level=10)
        other_subject = Subject.objects.create(
            course=other_course, name="Bio")
        now = timezone.now()
        other = LiveSession.objects.create(
            course=other_course, subject=other_subject, title="Cells",
            start_time=now, end_time=now + timedelta(hours=1),
            room_name="room_other_pol", created_by=self.teacher,
        )
        self.assertTrue(egress_svc.is_recording_enabled_for(self.session))
        self.assertFalse(egress_svc.is_recording_enabled_for(other))


@override_settings(**EGRESS_ON)
class FailsClosedTest(PolicyBase):
    """An unreadable settings row must never mean "record everything"."""

    def test_settings_read_failure_does_not_record(self):
        with patch.object(GlobalSettings, "load",
                          side_effect=RuntimeError("db gone")):
            self.assertFalse(egress_svc.is_recording_enabled_for(self.session))

    def test_settings_read_failure_does_not_raise_into_the_join_path(self):
        """A broken settings row must not stop a class going live."""
        with patch.object(GlobalSettings, "load",
                          side_effect=RuntimeError("db gone")):
            self.assertIsNone(egress_svc.start_session_egress(self.session))

    def test_course_override_still_wins_without_reading_globals(self):
        """A course that has been explicitly decided about does not need the
        global row at all — one less thing that can fail."""
        self.set_course(True)
        with patch.object(GlobalSettings, "load",
                          side_effect=RuntimeError("db gone")):
            self.assertTrue(egress_svc.is_recording_enabled_for(self.session))


class RecordingStateReasonTest(PolicyBase):
    """WHY recording is off, not just whether.

    A bare boolean made the admin panel advise "turn it on in Courses" for a
    server that simply had no LiveKit credentials — advice that could not
    work. Browser-testing the panel is what surfaced it.
    """

    def test_no_credentials_is_distinguishable_from_a_policy_decision(self):
        self.set_global(True)
        self.assertEqual(
            egress_svc.recording_state_for(self.session),
            egress_svc.RECORD_NO_INFRA,
        )

    @override_settings(**EGRESS_ON)
    def test_global_off_is_reported_as_such(self):
        self.set_global(False)
        self.assertEqual(
            egress_svc.recording_state_for(self.session),
            egress_svc.RECORD_GLOBAL_OFF,
        )

    @override_settings(**EGRESS_ON)
    def test_course_opt_out_is_reported_separately_from_the_global_default(self):
        self.set_global(True)
        self.set_course(False)
        self.assertEqual(
            egress_svc.recording_state_for(self.session),
            egress_svc.RECORD_COURSE_OFF,
        )

    @override_settings(**EGRESS_ON)
    def test_enabled_state(self):
        self.set_global(True)
        self.assertEqual(
            egress_svc.recording_state_for(self.session),
            egress_svc.RECORD_ENABLED,
        )

    @override_settings(**EGRESS_ON)
    def test_unreadable_settings_are_reported_as_unknown_not_as_off(self):
        """"Off by policy" and "we could not tell" need different messages;
        both still mean no recording."""
        with patch.object(GlobalSettings, "load",
                          side_effect=RuntimeError("db gone")):
            self.assertEqual(
                egress_svc.recording_state_for(self.session),
                egress_svc.RECORD_UNKNOWN,
            )
            self.assertFalse(egress_svc.is_recording_enabled_for(self.session))

    @override_settings(**EGRESS_ON)
    def test_the_boolean_can_never_disagree_with_the_state(self):
        """is_recording_enabled_for is DERIVED from recording_state_for, so
        the two cannot drift — which is the whole reason it was refactored
        rather than duplicated."""
        for setup in (
            lambda: self.set_global(True),
            lambda: self.set_global(False),
            lambda: (self.set_global(True), self.set_course(False)),
            lambda: (self.set_global(False), self.set_course(True)),
        ):
            self.set_course(None)
            setup()
            state = egress_svc.recording_state_for(self.session)
            self.assertEqual(
                egress_svc.is_recording_enabled_for(self.session),
                state == egress_svc.RECORD_ENABLED,
                f"boolean disagrees with state {state}",
            )


@override_settings(**EGRESS_ON)
class PolicyAtStartTest(PolicyBase):
    """The policy is enforced where the money is spent, not just in the
    helper."""

    def test_no_livekit_call_when_policy_says_no(self):
        self.set_global(False)
        with patch.object(egress_svc, "_start_room_composite") as call:
            self.assertIsNone(egress_svc.start_session_egress(self.session))
        self.assertFalse(call.called)
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_livekit_is_called_when_policy_says_yes(self):
        self.set_global(True)
        with patch.object(
            egress_svc, "_start_room_composite",
            return_value=SimpleNamespace(egress_id="EG_pol", status=1),
        ) as call:
            row = egress_svc.start_session_egress(self.session)
        self.assertTrue(call.called)
        self.assertEqual(row.egress_id, "EG_pol")

    def test_teacher_join_respects_a_course_opt_out(self):
        from livestream import views as lv

        self.set_global(True)
        self.set_course(False)
        evt = SimpleNamespace(
            room=SimpleNamespace(name="room_pol"),
            participant=SimpleNamespace(
                identity=build_identity(self.teacher.id, self.session.id)),
        )
        with patch.object(egress_svc, "_start_room_composite") as call:
            with self.captureOnCommitCallbacks(execute=True):
                lv._handle_participant_join(evt)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_LIVE)
        self.assertFalse(call.called)


@override_settings(**EGRESS_ON)
class AdminVisibilityTest(PolicyBase):
    """An admin looking at a class with no recording needs to tell "recording
    is off for this course" from "recording is on and failed"."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="pol_a@x.com", email="pol_a@x.com", password="x",
            is_staff=True, is_superuser=True)

    def _detail(self):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(user=self.admin)
        return c.get(f"/api/livestream/admin/streams/{self.session.id}/")

    def test_detail_reports_the_resolved_policy_and_the_reason(self):
        self.set_global(True)
        res = self._detail()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["auto_record_enabled"])
        self.assertEqual(res.data["auto_record_state"], egress_svc.RECORD_ENABLED)

        self.set_course(False)
        res = self._detail()
        self.assertFalse(res.data["auto_record_enabled"])
        # The reason is what lets the panel say something actionable.
        self.assertEqual(
            res.data["auto_record_state"], egress_svc.RECORD_COURSE_OFF)

    def test_detail_lists_every_attempt_with_its_failure(self):
        LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_a",
            status=LiveSessionEgress.STATUS_START_FAILED,
            error="livekit unreachable",
        )
        LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_b",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/x/y.mp4", file_size_bytes=1024,
        )
        rows = self._detail().data["egress"]
        self.assertEqual(len(rows), 2)
        by_id = {r["egress_id"]: r for r in rows}
        self.assertIn("livekit unreachable", by_id["EG_a"]["error"])
        self.assertTrue(by_id["EG_a"]["is_terminal"])
        self.assertTrue(by_id["EG_b"]["awaiting_stream_fetch"])

    def test_detail_shows_whether_the_raw_file_is_still_public(self):
        """raw_deleted_at NULL means the mp4 is still on the public pull zone —
        the single most important thing for an admin to be able to see."""
        LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_c",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/x/y.mp4",
        )
        row = self._detail().data["egress"][0]
        self.assertIsNone(row["raw_deleted_at"])

    def test_detail_has_an_empty_egress_list_when_nothing_ran(self):
        res = self._detail()
        self.assertEqual(res.data["egress"], [])
        self.assertFalse(res.data["auto_record_enabled"])
