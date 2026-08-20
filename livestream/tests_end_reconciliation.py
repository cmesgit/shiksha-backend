# Ending a class must close the attendance record and the LiveKit room.
#
# There are four ways a LiveSession ends, and they had drifted apart. Two of
# them left work undone, and both failures were silent:
#
#   end_live_session (the teacher's "End class" button — by far the most
#   common) closed the LiveKit room but not the attendance intervals. It
#   relied on LiveKit round-tripping a webhook back to close them. Nothing
#   repairs a miss: the reconcile sweep only scans non-terminal sessions, so
#   once the row is COMPLETED it is never looked at again. An open interval
#   reads as 0 seconds, so a student who attended the whole class appears on
#   the teacher's roster as 0 minutes, permanently.
#
#   auto_complete_expired_sessions (the clock) closed neither. Everyone still
#   connected kept publishing media until their 2h token expired — the UI
#   said the class was over while the call carried on, still billing minutes.
#
# These tests pin both, and assert the two paths agree with each other.

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream.models import (
    LiveSession,
    LiveSessionAttendanceInterval,
)
from livestream.services import attendance as attendance_svc

User = get_user_model()


class EndReconciliationTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="er_t@x.com", email="er_t@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=self.teacher, role=role,
                                is_active=True, is_primary=True)

        board = Board.objects.create(name="ERBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="ER10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Chem")
        TeachingAssignment.objects.create(subject=self.subject, teacher=self.teacher,
                                          batch=None, is_active=True)

        self.student = User.objects.create_user(
            username="er_s@x.com", email="er_s@x.com", password="x")
        self.profile = LearnerProfile.objects.create(
            account=self.student, display_name="S", full_name="S",
            student_id="ER001", is_default=True)
        Enrollment.objects.create(user=self.student, learner_profile=self.profile,
                                  course=self.course,
                                  status=Enrollment.STATUS_ACTIVE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Live",
            start_time=now - timedelta(minutes=30),
            end_time=now + timedelta(hours=1),
            room_name="room_er", created_by=self.teacher,
            status=LiveSession.STATUS_LIVE,
        )
        # The student has been in the room for half an hour and has not left,
        # which is the normal state when a teacher clicks End.
        attendance_svc.open_interval(self.session, self.student,
                                     when=now - timedelta(minutes=30))

    def _open_intervals(self):
        return LiveSessionAttendanceInterval.objects.filter(
            session=self.session, left_at__isnull=True).count()

    def test_teacher_ending_class_closes_attendance(self):
        self.assertEqual(self._open_intervals(), 1)
        client = APIClient()
        client.force_authenticate(user=self.teacher, token={"context": "teacher"})
        with patch("livestream.views.close_room"):
            r = client.post(f"/api/livestream/sessions/{self.session.id}/end/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._open_intervals(), 0,
                         "attendance left open — the student will read as 0 minutes")

    def test_student_who_stayed_is_not_credited_zero_minutes(self):
        """The failure as the teacher actually experiences it: a full-class
        attendee showing 0 on the roster."""
        client = APIClient()
        client.force_authenticate(user=self.teacher, token={"context": "teacher"})
        with patch("livestream.views.close_room"):
            client.post(f"/api/livestream/sessions/{self.session.id}/end/")
        iv = LiveSessionAttendanceInterval.objects.get(session=self.session,
                                                       user=self.student)
        self.assertIsNotNone(iv.left_at)
        self.assertGreater(iv.duration_seconds(), 0)

    def test_clock_expiry_closes_attendance_and_the_room(self):
        from livestream.tasks import auto_complete_expired_sessions

        # Past the overrun grace, not merely past end_time: a class is allowed
        # to run a little long (LiveSession.LIVE_GRACE) before the sweep is
        # entitled to end it.
        LiveSession.objects.filter(pk=self.session.pk).update(
            end_time=timezone.now() - LiveSession.LIVE_GRACE - timedelta(minutes=1))
        with patch("livestream.services.room_admin.close_room") as close_room:
            auto_complete_expired_sessions()

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_COMPLETED)
        self.assertEqual(self._open_intervals(), 0,
                         "clock expiry left attendance open with no sweep to repair it")
        self.assertTrue(close_room.called,
                        "LiveKit room left open — the call continues after the "
                        "class is marked over")
        self.assertIsNotNone(self.session.actual_ended_at)

    def test_a_failing_livekit_call_does_not_abort_the_sweep(self):
        """One unreachable room must not stop the rest of the batch being
        swept — that would turn a LiveKit blip into a pile of stuck rows."""
        from livestream.tasks import auto_complete_expired_sessions

        # Past the overrun grace, not merely past end_time: a class is allowed
        # to run a little long (LiveSession.LIVE_GRACE) before the sweep is
        # entitled to end it.
        LiveSession.objects.filter(pk=self.session.pk).update(
            end_time=timezone.now() - LiveSession.LIVE_GRACE - timedelta(minutes=1))
        with patch("livestream.services.room_admin.close_room",
                   side_effect=RuntimeError("livekit unreachable")):
            auto_complete_expired_sessions()

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_COMPLETED)
        self.assertEqual(self._open_intervals(), 0)
