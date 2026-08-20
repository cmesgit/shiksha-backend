# The webhook-level defects: a lost update, a premature "LIVE" blast, and a
# participant identity that collided across tabs.

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream import views as lv
from livestream.models import (
    LiveSession,
    LiveSessionAttendanceInterval,
)
from livestream.services.token import build_identity, parse_identity

User = get_user_model()


def _evt(room, identity):
    return SimpleNamespace(room=SimpleNamespace(name=room),
                           participant=SimpleNamespace(identity=identity))


class WebhookRaceBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="wr_t@x.com", email="wr_t@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=self.teacher, role=role, is_active=True,
                                is_primary=True)
        board = Board.objects.create(name="WRBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="WR10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Maths")
        self.batch = Batch.objects.create(course=self.course, name="WR-A")
        TeachingAssignment.objects.create(subject=self.subject, teacher=self.teacher,
                                          batch=None, is_active=True)

        self.student = User.objects.create_user(
            username="wr_s@x.com", email="wr_s@x.com", password="x")
        self.profile = LearnerProfile.objects.create(
            account=self.student, display_name="S", full_name="S",
            student_id="WR1", is_default=True)
        Enrollment.objects.create(user=self.student, learner_profile=self.profile,
                                  course=self.course, batch=self.batch,
                                  status=Enrollment.STATUS_ACTIVE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Maths", start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1), room_name="room_wr",
            created_by=self.teacher, status=LiveSession.STATUS_LIVE,
        )

    def tid(self):
        return build_identity(self.teacher.id, self.session.id)

    def sid(self):
        return build_identity(self.student.id, self.session.id)


class StudentLeaveCannotRevertTeacherRejoinTests(WebhookRaceBase):
    """H3. participant_left saved teacher_left_at and status unconditionally —
    for every participant. A student leaving therefore wrote back whatever
    those columns held when its handler read the row. Interleaved with the
    teacher's rejoin it resurrected a stale "teacher is gone" timer, and the
    status sweep then walked that to COMPLETED 60 minutes later, force-ending
    a class that was still running."""

    def test_student_leaving_does_not_touch_the_teacher_timer(self):
        self.session.teacher_left_at = None
        self.session.status = LiveSession.STATUS_LIVE
        self.session.save(update_fields=["teacher_left_at", "status"])

        lv._handle_participant_left(_evt("room_wr", self.sid()))

        self.session.refresh_from_db()
        self.assertIsNone(self.session.teacher_left_at,
                          "a student's leave set the teacher-gone timer")
        self.assertEqual(self.session.status, LiveSession.STATUS_LIVE,
                         "a student's leave knocked the class out of LIVE")

    def test_a_stale_student_leave_cannot_overwrite_a_fresh_rejoin(self):
        """The race itself: the student's handler holds a row read from BEFORE
        the teacher rejoined, and must not flush it back."""
        stale = LiveSession.objects.get(pk=self.session.pk)
        stale.teacher_left_at = timezone.now() - timedelta(minutes=5)
        stale.status = LiveSession.STATUS_RECONNECTING

        # Teacher rejoins and commits LIVE.
        lv._handle_participant_join(_evt("room_wr", self.tid()))

        # Now the student's leave lands, carrying the stale view.
        with patch.object(lv.LiveSession.objects, "select_for_update") as sfu:
            sfu.return_value.filter.return_value.first.return_value = stale
            lv._handle_participant_left(_evt("room_wr", self.sid()))

        fresh = LiveSession.objects.get(pk=self.session.pk)
        self.assertIsNone(fresh.teacher_left_at,
                          "stale student-leave revived the teacher-gone timer")
        self.assertEqual(fresh.status, LiveSession.STATUS_LIVE)

    def test_teacher_leaving_still_starts_the_timer(self):
        """The guard must not disable the behaviour it is narrowing."""
        lv._handle_participant_left(_evt("room_wr", self.tid()))
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.teacher_left_at)
        self.assertEqual(self.session.status, LiveSession.STATUS_RECONNECTING)


class EarlyLiveNotificationTests(WebhookRaceBase):
    """M2. The teacher branch of join has no time gate, so opening the room
    early pushed "🔴 LIVE!" to the whole batch — while the student join gate
    refuses anyone until start_time − 15min. Students tapped it and were told
    "Too early"."""

    def test_no_blast_when_students_still_cannot_join(self):
        LiveSession.objects.filter(pk=self.session.pk).update(
            start_time=timezone.now() + timedelta(hours=6),
            end_time=timezone.now() + timedelta(hours=7))
        with patch("notifications.services.notify") as notify:
            lv._handle_participant_join(_evt("room_wr", self.tid()))
        self.assertFalse(notify.called,
                         "whole batch told a class was live 6 hours early")

    def test_blast_still_fires_inside_the_join_window(self):
        LiveSession.objects.filter(pk=self.session.pk).update(
            start_time=timezone.now() + timedelta(minutes=5),
            end_time=timezone.now() + timedelta(hours=1))
        with patch("notifications.services.notify") as notify:
            lv._handle_participant_join(_evt("room_wr", self.tid()))
        self.assertTrue(notify.called,
                        "students were never told the class started")


class ParticipantIdentityTests(WebhookRaceBase):
    """M4. Livestream was the only feature using a bare user id as the LiveKit
    identity; group and private both use a composite and say why. LiveKit
    replaces a duplicate identity, so a second tab silently killed the first."""

    def test_identity_round_trips(self):
        ident = build_identity(self.student.id, self.session.id)
        self.assertNotEqual(ident, str(self.student.id))
        self.assertEqual(parse_identity(ident), str(self.student.id))

    def test_legacy_bare_identity_is_still_understood(self):
        """Tokens live 2h, so a deploy mid-class leaves participants holding
        the old form. Failing to parse those would corrupt their attendance."""
        self.assertEqual(parse_identity(str(self.student.id)),
                         str(self.student.id))

    def test_webhook_with_composite_identity_opens_the_right_interval(self):
        lv._handle_participant_join(_evt("room_wr", self.sid()))
        self.assertTrue(
            LiveSessionAttendanceInterval.objects.filter(
                session=self.session, user=self.student,
                left_at__isnull=True).exists(),
            "composite identity was not resolved back to the student")

    def test_reconcile_sweep_does_not_evict_present_participants(self):
        """The sweep compares DB user ids against LiveKit identities. Without
        parsing, nothing matches and it closes EVERY open interval each
        minute — wiping attendance for everyone actually in the class."""
        from livestream.tasks import sample_live_viewers
        lv._handle_participant_join(_evt("room_wr", self.sid()))

        with patch("livestream.tasks._livekit_room_identities",
                   return_value=[self.sid()]):
            sample_live_viewers()

        self.assertTrue(
            LiveSessionAttendanceInterval.objects.filter(
                session=self.session, user=self.student,
                left_at__isnull=True).exists(),
            "sweep evicted a student who was still in the room")
