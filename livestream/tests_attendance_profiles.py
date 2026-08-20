# Attendance must be per LEARNER PROFILE, not per account.
#
# One email is one account holding many LearnerProfiles — a parent and their
# children. Attendance keyed on `user` alone summed two siblings' watch time
# into one row that then appeared as BOTH children's attendance: a parent
# checking either child's record saw the other's minutes folded in, and a
# teacher's roster counted one attendee where two were present.

from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import LearnerProfile
from courses.models import Batch, Board, Course, Subject
from enrollments.models import Enrollment
from livestream import views as lv
from livestream.models import (
    LiveSession,
    LiveSessionAttendance,
    LiveSessionAttendanceInterval,
)
from livestream.services import attendance as attendance_svc
from livestream.services.token import build_identity, parse_profile_id

User = get_user_model()


def _evt(room, identity):
    return SimpleNamespace(room=SimpleNamespace(name=room),
                           participant=SimpleNamespace(identity=identity))


class SiblingAttendanceTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="ap_t@x.com", email="ap_t@x.com", password="x")
        board = Board.objects.create(name="APBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="AP10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Hist")
        self.batch = Batch.objects.create(course=self.course, name="AP-A")

        # ONE account, TWO children — the shape this is all about.
        self.parent = User.objects.create_user(
            username="ap_p@x.com", email="ap_p@x.com", password="x")
        self.child_a = LearnerProfile.objects.create(
            account=self.parent, display_name="A", full_name="A",
            student_id="APA", is_default=True)
        self.child_b = LearnerProfile.objects.create(
            account=self.parent, display_name="B", full_name="B",
            student_id="APB")
        for p in (self.child_a, self.child_b):
            Enrollment.objects.create(user=self.parent, learner_profile=p,
                                      course=self.course, batch=self.batch,
                                      status=Enrollment.STATUS_ACTIVE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Hist", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), room_name="room_ap",
            created_by=self.teacher, status=LiveSession.STATUS_LIVE,
        )

    def test_two_children_get_two_attendance_rows(self):
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_a)
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_b)
        attendance_svc.close_intervals(self.session, self.parent,
                                       learner_profile=self.child_a)
        attendance_svc.close_intervals(self.session, self.parent,
                                       learner_profile=self.child_b)

        rows = LiveSessionAttendance.objects.filter(session=self.session)
        self.assertEqual(rows.count(), 2,
                         "siblings collapsed into one attendance row")
        self.assertEqual(
            {r.learner_profile_id for r in rows},
            {self.child_a.id, self.child_b.id})

    def test_one_child_leaving_does_not_close_the_other(self):
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_a)
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_b)
        attendance_svc.close_intervals(self.session, self.parent,
                                       learner_profile=self.child_a)

        still_open = LiveSessionAttendanceInterval.objects.filter(
            session=self.session, left_at__isnull=True)
        self.assertEqual(still_open.count(), 1)
        self.assertEqual(still_open.first().learner_profile_id, self.child_b.id)

    def test_watch_time_is_not_merged(self):
        start = timezone.now() - timedelta(minutes=60)
        attendance_svc.open_interval(self.session, self.parent, when=start,
                                     learner_profile=self.child_a)
        attendance_svc.close_intervals(
            self.session, self.parent, when=start + timedelta(minutes=50),
            learner_profile=self.child_a)
        attendance_svc.open_interval(self.session, self.parent, when=start,
                                     learner_profile=self.child_b)
        attendance_svc.close_intervals(
            self.session, self.parent, when=start + timedelta(minutes=10),
            learner_profile=self.child_b)

        a = LiveSessionAttendance.objects.get(session=self.session,
                                              learner_profile=self.child_a)
        b = LiveSessionAttendance.objects.get(session=self.session,
                                              learner_profile=self.child_b)
        self.assertAlmostEqual(a.total_seconds, 50 * 60, delta=5)
        self.assertAlmostEqual(b.total_seconds, 10 * 60, delta=5)
        self.assertNotEqual(a.total_seconds, b.total_seconds,
                            "one child's watch time was credited to the other")

    def test_both_children_count_as_two_in_the_room(self):
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_a)
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_b)
        self.assertEqual(attendance_svc.current_watching(self.session), 2,
                         "a family account counted as one attendee")

    def test_webhook_identity_carries_the_profile_end_to_end(self):
        ident = build_identity(self.parent.id, self.session.id, self.child_b.id)
        self.assertEqual(parse_profile_id(ident), str(self.child_b.id))

        lv._handle_participant_join(_evt("room_ap", ident))
        iv = LiveSessionAttendanceInterval.objects.get(session=self.session)
        self.assertEqual(str(iv.learner_profile_id), str(self.child_b.id),
                         "the webhook lost track of which child joined")

    def test_legacy_identity_does_not_invent_a_profile(self):
        """A two-segment identity's middle field is the SESSION id. Treating it
        as a profile id would write attendance against a profile that does not
        exist — worse than recording none."""
        self.assertIsNone(parse_profile_id(f"{self.parent.id}_{self.session.id}"))
        self.assertIsNone(parse_profile_id(str(self.parent.id)))

    def test_teacher_rows_stay_profileless_and_do_not_collide(self):
        attendance_svc.open_interval(self.session, self.teacher)
        attendance_svc.open_interval(self.session, self.teacher)  # rejoin
        rows = LiveSessionAttendance.objects.filter(session=self.session,
                                                    user=self.teacher)
        self.assertEqual(rows.count(), 1,
                         "teacher rejoin created a duplicate rollup row")
        self.assertIsNone(rows.first().learner_profile_id)

    def test_removing_the_account_closes_every_child_interval(self):
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_a)
        attendance_svc.open_interval(self.session, self.parent,
                                     learner_profile=self.child_b)
        attendance_svc.close_user(self.session, self.parent)
        self.assertEqual(
            LiveSessionAttendanceInterval.objects.filter(
                session=self.session, left_at__isnull=True).count(), 0,
            "a removed account left intervals open, reading as 0 minutes")
