# The live-session lifecycle fixes, pinned.
#
# Each class here corresponds to a confirmed defect that made running a real
# 6-12 month batch impractical. The comments say what the teacher or student
# actually saw, because that is what these tests exist to prevent recurring.

from datetime import timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream.models import LiveSession, LiveSessionRemoval

User = get_user_model()


class LiveBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="lh_t@x.com", email="lh_t@x.com", password="x")
        self.substitute = User.objects.create_user(
            username="lh_sub@x.com", email="lh_sub@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        for t in (self.teacher, self.substitute):
            UserRole.objects.create(user=t, role=role, is_active=True,
                                    is_primary=True)

        board = Board.objects.create(name="LHBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="LH10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Bio")
        self.batch = Batch.objects.create(course=self.course, name="LH-A")
        # Both teachers are assigned to the subject; only `teacher` creates
        # the session. The schema has always had a SUBSTITUTE role — only one
        # active PRIMARY is allowed per subject — which is what makes the old
        # `is_teacher = is_creator` a bug rather than a policy: the data model
        # anticipated cover teachers and the live-class code ignored them.
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=self.teacher, batch=None,
            is_active=True, role=TeachingAssignment.ROLE_PRIMARY)
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=self.substitute, batch=None,
            is_active=True, role=TeachingAssignment.ROLE_SUBSTITUTE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Bio class",
            start_time=now - timedelta(minutes=20),
            end_time=now + timedelta(minutes=40),
            room_name="room_lh", created_by=self.teacher,
            status=LiveSession.STATUS_LIVE,
        )

    def teacher_client(self, who=None):
        c = APIClient()
        c.force_authenticate(user=who or self.teacher, token={"context": "teacher"})
        return c

    def make_student(self, tag="s1"):
        u = User.objects.create_user(username=f"lh_{tag}@x.com",
                                     email=f"lh_{tag}@x.com", password="x")
        p = LearnerProfile.objects.create(
            account=u, display_name=tag, full_name=tag,
            student_id=f"LH{tag}", is_default=True)
        # Enrol into the SESSION'S batch. Without this the batch gate rejects
        # the join with its own 403, which would make the removal tests below
        # pass for entirely the wrong reason — they would be asserting the
        # batch guard, not the removal, and would keep passing if removal
        # were deleted outright.
        Enrollment.objects.create(user=u, learner_profile=p, course=self.course,
                                  batch=self.batch,
                                  status=Enrollment.STATUS_ACTIVE)
        return u, p


class EmptyRoomDoesNotKillTheClassTests(LiveBase):
    """C1. A teacher joined at 09:50 to test their mic for a 10:00 class and
    left; the room went empty, LiveKit closed it, and the room_finished
    webhook marked the class COMPLETED for good. At 10:00 the teacher's own
    join was refused with "Session has ended." — and nothing could undo it."""

    def _fire_room_finished(self):
        # The webhook handler reads the event with getattr, not dict access
        # (LiveKit delivers a protobuf object), so the fixture must be an
        # object too — a dict silently yields no room name and matches
        # nothing, which would make these tests pass for the wrong reason.
        from types import SimpleNamespace
        from livestream.views import _handle_room_finished
        _handle_room_finished(
            SimpleNamespace(room=SimpleNamespace(name=self.session.room_name)))

    def test_empty_room_before_the_end_does_not_complete_the_class(self):
        self._fire_room_finished()
        self.session.refresh_from_db()
        self.assertNotEqual(
            self.session.status, LiveSession.STATUS_COMPLETED,
            "an empty room mid-class permanently ended the lesson")

    def test_empty_room_still_reconciles_attendance(self):
        """The useful half of the webhook must survive the fix: a closed room
        really does mean nobody is connected."""
        from livestream.services import attendance as attendance_svc
        from livestream.models import LiveSessionAttendanceInterval
        student, _ = self.make_student()
        attendance_svc.open_interval(self.session, student,
                                     when=timezone.now() - timedelta(minutes=10))
        self._fire_room_finished()
        self.assertEqual(
            LiveSessionAttendanceInterval.objects.filter(
                session=self.session, left_at__isnull=True).count(), 0)

    def test_empty_room_after_the_end_does_complete_the_class(self):
        LiveSession.objects.filter(pk=self.session.pk).update(
            end_time=timezone.now() - timedelta(hours=2))
        self.session.refresh_from_db()
        self._fire_room_finished()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_COMPLETED)


class OverrunGraceTests(LiveBase):
    """C2. A 10:00-11:00 class running to 11:10 flipped to Completed at 11:00.
    The room stayed open so the lesson carried on — but a student whose wifi
    blipped was told the session had ended."""

    def test_student_can_rejoin_just_past_the_planned_end(self):
        student, profile = self.make_student()
        LiveSession.objects.filter(pk=self.session.pk).update(
            end_time=timezone.now() - timedelta(minutes=5))
        c = APIClient()
        c.force_authenticate(user=student, token={
            "context": "learner", "active_profile": str(profile.id)})
        with patch("enrollments.services.has_active_subscription", return_value=True), \
             patch("livestream.views.generate_livekit_token", return_value="tok"):
            r = c.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertNotEqual(
            r.status_code, 400,
            "student locked out of a class that is still running")

    def test_past_the_grace_the_class_really_is_over(self):
        student, profile = self.make_student("s2")
        LiveSession.objects.filter(pk=self.session.pk).update(
            end_time=timezone.now() - timedelta(hours=3))
        c = APIClient()
        c.force_authenticate(user=student, token={
            "context": "learner", "active_profile": str(profile.id)})
        r = c.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 400)

    def test_teacher_can_extend_a_running_class(self):
        r = self.teacher_client().post(
            f"/api/livestream/sessions/{self.session.id}/extend/",
            {"minutes": 20}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.extended_until)

    def test_extension_is_capped(self):
        r = self.teacher_client().post(
            f"/api/livestream/sessions/{self.session.id}/extend/",
            {"minutes": 60 * 24}, format="json")
        self.assertEqual(r.status_code, 400)


class SubstituteTeacherTests(LiveBase):
    """H4. A covering teacher could enter the room but got the viewer grant —
    mic only, no camera, no screen share — and a 403 from End."""

    def test_substitute_gets_a_presenter_token(self):
        captured = {}

        def fake_token(user, session, is_teacher, **kw):
            captured["is_teacher"] = is_teacher
            return "tok"

        with patch("livestream.views.generate_livekit_token",
                   side_effect=fake_token):
            r = self.teacher_client(self.substitute).post(
                f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(captured.get("is_teacher"),
                        "substitute got a viewer grant — no camera, no slides")

    def test_substitute_can_end_the_class(self):
        with patch("livestream.views.close_room"):
            r = self.teacher_client(self.substitute).post(
                f"/api/livestream/sessions/{self.session.id}/end/")
        self.assertEqual(r.status_code, 200, r.content)


class ModerationTests(LiveBase):
    """M3. There was no way to mute or eject anyone, and no revocation: a
    LiveKit token is a bearer credential, so even disconnecting a student let
    them rejoin with the token already in their browser."""

    def test_removed_student_cannot_rejoin(self):
        student, profile = self.make_student("s3")
        with patch("livestream.services.room_admin.remove_participant"):
            r = self.teacher_client().post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(student.id)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        c = APIClient()
        c.force_authenticate(user=student, token={
            "context": "learner", "active_profile": str(profile.id)})
        with patch("enrollments.services.has_active_subscription", return_value=True), \
             patch("livestream.views.generate_livekit_token", return_value="tok"):
            r = c.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 403,
                         "removal wore off the moment the student refreshed")
        # Assert the REASON, not just the code: this student would also be
        # 403'd by the batch gate or the too-early gate, and a bare status
        # check would keep passing even if removal stopped working entirely.
        self.assertIn("removed", r.json().get("detail", "").lower())

    def test_removal_is_recorded_even_if_livekit_is_down(self):
        """The bar must be written before the disconnect call, so a LiveKit
        failure cannot leave a 'successful' removal that does nothing."""
        student, _ = self.make_student("s4")
        with patch("livestream.services.room_admin.remove_participant",
                   side_effect=RuntimeError("livekit down")):
            r = self.teacher_client().post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(student.id)}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["disconnected"])
        self.assertTrue(LiveSessionRemoval.objects.filter(
            session=self.session, user=student, revoked_at__isnull=True).exists())

    def test_readmit_lets_them_back(self):
        student, profile = self.make_student("s5")
        LiveSessionRemoval.objects.create(session=self.session, user=student,
                                          removed_by=self.teacher)
        r = self.teacher_client().post(
            f"/api/livestream/sessions/{self.session.id}/readmit/",
            {"user_id": str(student.id)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        c = APIClient()
        c.force_authenticate(user=student, token={
            "context": "learner", "active_profile": str(profile.id)})
        with patch("enrollments.services.has_active_subscription", return_value=True), \
             patch("livestream.views.generate_livekit_token", return_value="tok"):
            r = c.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertNotEqual(r.status_code, 403)

    def test_a_student_cannot_remove_anyone(self):
        victim, _ = self.make_student("s6")
        attacker, ap = self.make_student("s7")
        c = APIClient()
        c.force_authenticate(user=attacker, token={
            "context": "learner", "active_profile": str(ap.id)})
        r = c.post(f"/api/livestream/sessions/{self.session.id}/remove/",
                   {"user_id": str(victim.id)}, format="json")
        self.assertIn(r.status_code, (401, 403))
        self.assertFalse(LiveSessionRemoval.objects.exists())

    def test_the_session_teacher_cannot_be_removed(self):
        r = self.teacher_client(self.substitute).post(
            f"/api/livestream/sessions/{self.session.id}/remove/",
            {"user_id": str(self.teacher.id)}, format="json")
        self.assertEqual(r.status_code, 400)


class RecurringSchedulingTests(LiveBase):
    """A 6-month batch previously meant ~50 trips through the create form."""

    # `days_of_week` is IST, because that is the timezone the teacher picks
    # them in (TIME_ZONE = Asia/Kolkata, and DRF parses start_time into it).
    # Django hands datetimes back from the DB in UTC, so every assertion here
    # goes through localtime() — comparing raw .weekday() compares UTC, which
    # is a DIFFERENT DAY whenever the class sits between 00:00 and 05:30 IST.
    @staticmethod
    def _weekday(dt):
        return timezone.localtime(dt).weekday()

    def _post(self, **over):
        # Anchored at 10:00 IST tomorrow rather than "now + 1 day": with a
        # wall-clock time-of-day these tests passed or failed depending on the
        # hour the suite happened to run (a 00:00-05:30 IST class is the
        # previous day in UTC), which is a ~5.5-hour window of false failures.
        start = (timezone.localtime(timezone.now()) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        body = {
            "title": "Weekly Bio",
            "subject_id": str(self.subject.id),
            "batch_id": str(self.batch.id),
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "repeat": "weekly",
            "count": 24,
        }
        body.update(over)
        return self.teacher_client().post(
            "/api/livestream/sessions/recurring/", body, format="json")

    def test_creates_a_whole_term_in_one_call(self):
        r = self._post()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["created_count"], 24)
        self.assertEqual(
            LiveSession.objects.filter(series_id=r.json()["series_id"]).count(), 24)

    def test_weekly_lands_on_the_same_weekday(self):
        r = self._post()
        rows = LiveSession.objects.filter(
            series_id=r.json()["series_id"]).order_by("start_time")
        weekdays = {self._weekday(s.start_time) for s in rows}
        self.assertEqual(len(weekdays), 1, "weekly series drifted across weekdays")

    def test_twice_weekly(self):
        r = self._post(days_of_week=[0, 3], count=10)
        self.assertEqual(r.status_code, 201, r.content)
        rows = LiveSession.objects.filter(series_id=r.json()["series_id"])
        self.assertEqual({self._weekday(s.start_time) for s in rows}, {0, 3})

    def test_the_weekday_a_teacher_picks_is_the_weekday_in_IST(self):
        # The regression the two tests above were hiding. A class scheduled
        # early enough in the IST morning is the PREVIOUS day in UTC, so
        # asserting on raw .weekday() silently compared the wrong day and made
        # this suite fail for ~5.5 hours out of every 24. Scheduling itself was
        # always correct: _occurrence_starts filters on an IST-localised
        # cursor. This pins the contract so the timezone can't drift back.
        start = (timezone.localtime(timezone.now()) + timedelta(days=1)).replace(
            hour=1, minute=0, second=0, microsecond=0)   # 01:00 IST = 19:30 UTC, previous day
        r = self._post(
            start_time=start.isoformat(),
            end_time=(start + timedelta(hours=1)).isoformat(),
            days_of_week=[0, 3], count=6)
        self.assertEqual(r.status_code, 201, r.content)
        rows = LiveSession.objects.filter(series_id=r.json()["series_id"])
        self.assertTrue(rows.exists())
        self.assertEqual({self._weekday(s.start_time) for s in rows}, {0, 3})
        # And prove the trap is real: in UTC these same rows are NOT Mon/Thu.
        self.assertEqual({s.start_time.astimezone(dt_timezone.utc).weekday()
                          for s in rows}, {6, 2})

    def test_a_clash_skips_that_date_instead_of_failing_everything(self):
        first = self._post(count=3)
        self.assertEqual(first.status_code, 201)
        # Same pattern again: every date now collides.
        second = self._post(count=3)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["created_count"], 0)
        self.assertEqual(second.json()["skipped_count"], 3)
        self.assertIn("clashes", second.json()["skipped"][0]["reason"])

    def test_occurrence_count_is_capped(self):
        r = self._post(repeat="daily", count=5000)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertLessEqual(r.json()["created_count"],
                             200, "no cap on generated sessions")

    def test_requires_an_end_condition(self):
        r = self._post()
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertTrue(body["series_id"])
        r2 = self.teacher_client().post("/api/livestream/sessions/recurring/", {
            "title": "No end", "subject_id": str(self.subject.id),
            "batch_id": str(self.batch.id),
            "start_time": (timezone.now() + timedelta(days=2)).isoformat(),
            "end_time": (timezone.now() + timedelta(days=2, hours=1)).isoformat(),
            "repeat": "weekly",
        }, format="json")
        self.assertEqual(r2.status_code, 400)

    def test_cancelling_a_series_spares_the_past(self):
        r = self._post(count=5)
        series = r.json()["series_id"]
        # Backdate one occurrence so it counts as already held.
        oldest = LiveSession.objects.filter(series_id=series).order_by("start_time").first()
        LiveSession.objects.filter(pk=oldest.pk).update(
            start_time=timezone.now() - timedelta(days=3),
            end_time=timezone.now() - timedelta(days=3) + timedelta(hours=1))

        r2 = self.teacher_client().post(
            f"/api/livestream/sessions/series/{series}/cancel/")
        self.assertEqual(r2.status_code, 200, r2.content)
        oldest.refresh_from_db()
        self.assertNotEqual(oldest.status, LiveSession.STATUS_CANCELLED,
                            "cancelling the series rewrote a class already held")
        self.assertEqual(
            LiveSession.objects.filter(series_id=series,
                                       status=LiveSession.STATUS_CANCELLED).count(), 4)

    def test_a_teacher_not_assigned_cannot_bulk_create(self):
        outsider = User.objects.create_user(username="lh_out@x.com",
                                            email="lh_out@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=outsider, role=role, is_active=True,
                                is_primary=True)
        c = APIClient()
        c.force_authenticate(user=outsider, token={"context": "teacher"})
        r = c.post("/api/livestream/sessions/recurring/", {
            "title": "Sneak", "subject_id": str(self.subject.id),
            "batch_id": str(self.batch.id),
            "start_time": (timezone.now() + timedelta(days=1)).isoformat(),
            "end_time": (timezone.now() + timedelta(days=1, hours=1)).isoformat(),
            "repeat": "weekly", "count": 5,
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(LiveSession.objects.filter(title="Sneak").exists())
