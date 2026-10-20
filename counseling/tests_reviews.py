"""Tests for session reviews and reschedule (workflow §8, §11-Reviews).

The headline thing being proved here is that `CounselorProfile.avg_rating`
and `.rating_count` MOVE. Before SessionReview existed they were declared,
serialized and sorted on but written by no code path in the repo, so
PublicStatsView always reported 0.0 and the directory's
order_by("-avg_rating") was a no-op.

The aggregate is RECOMPUTED from the reviews table rather than incremented,
so the edit and delete cases below are the ones that matter most — an
incrementing counter passes the "create one review" test and drifts forever
after.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import LearnerProfile
from counseling.models import Appointment, CounselorProfile, SessionReview

User = get_user_model()


def url_review(a): return f"/api/counseling/appointments/{a}/review/"
def url_resched(a): return f"/api/counseling/appointments/{a}/reschedule/"


class ReviewTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.student = User.objects.create_user(
            email="rv-student@example.com", username="rvstudent", password="x" * 14)
        cls.learner = LearnerProfile.objects.create(
            account=cls.student, display_name="Aditya Mehra", is_default=True)
        cls.other = User.objects.create_user(
            email="rv-other@example.com", username="rvother", password="x" * 14)
        LearnerProfile.objects.create(
            account=cls.other, display_name="Someone Else", is_default=True)

        cls.cuser = User.objects.create_user(
            email="rv-counsellor@example.com", username="rvcounsellor", password="x" * 14)
        cls.counselor = CounselorProfile.objects.create(
            user=cls.cuser, display_name="Dr. Sarah Lee",
            status=CounselorProfile.STATUS_APPROVED, is_listed=True)

    def make_appt(self, status=Appointment.STATUS_COMPLETED, when=None):
        return Appointment.objects.create(
            learner_profile=self.learner, booked_by=self.student,
            counselor=self.counselor,
            scheduled_at=when or (timezone.now() - timedelta(days=1)),
            status=status,
        )

    def refresh_counselor(self):
        self.counselor.refresh_from_db()
        return float(self.counselor.avg_rating), self.counselor.rating_count


class SessionReviewTests(ReviewTestBase):
    def setUp(self):
        self.client.force_authenticate(self.student)

    # ── the aggregate actually moves ────────────────────────────────────

    def test_creating_a_review_updates_the_counsellor_aggregate(self):
        self.assertEqual(self.refresh_counselor(), (0.0, 0))
        appt = self.make_appt()
        res = self.client.post(url_review(appt.id),
                               {"rating": 5, "comment": "Really helpful."}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.refresh_counselor(), (5.0, 1))

    def test_aggregate_is_the_mean_across_reviews(self):
        for rating in (5, 4, 3):
            appt = self.make_appt()
            SessionReview.objects.create(
                appointment=appt, counselor=self.counselor, rating=rating)
        avg, n = self.refresh_counselor()
        self.assertEqual(n, 3)
        self.assertAlmostEqual(avg, 4.0, places=2)

    def test_editing_a_review_recomputes_rather_than_accumulates(self):
        """An incrementing counter passes 'create one review' and breaks
        here — the count would go to 2 and the average would be wrong."""
        appt = self.make_appt()
        self.client.post(url_review(appt.id), {"rating": 5}, format="json")
        self.assertEqual(self.refresh_counselor(), (5.0, 1))

        res = self.client.post(url_review(appt.id),
                               {"rating": 2, "comment": "Changed my mind."},
                               format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.refresh_counselor(), (2.0, 1))

    def test_deleting_a_review_recomputes(self):
        a1, a2 = self.make_appt(), self.make_appt()
        self.client.post(url_review(a1.id), {"rating": 5}, format="json")
        self.client.post(url_review(a2.id), {"rating": 1}, format="json")
        self.assertEqual(self.refresh_counselor(), (3.0, 2))

        res = self.client.delete(url_review(a2.id))
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self.refresh_counselor(), (5.0, 1))

    def test_deleting_the_last_review_returns_to_zero(self):
        appt = self.make_appt()
        self.client.post(url_review(appt.id), {"rating": 4}, format="json")
        self.client.delete(url_review(appt.id))
        self.assertEqual(self.refresh_counselor(), (0.0, 0))

    # ── who may review what ─────────────────────────────────────────────

    def test_cannot_review_a_session_that_is_not_completed(self):
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED,
                              when=timezone.now() + timedelta(days=2))
        res = self.client.post(url_review(appt.id), {"rating": 5}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.refresh_counselor(), (0.0, 0))

    def test_cannot_review_someone_elses_appointment(self):
        appt = self.make_appt()
        self.client.force_authenticate(self.other)
        res = self.client.post(url_review(appt.id), {"rating": 1}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.refresh_counselor(), (0.0, 0))

    def test_rating_must_be_between_1_and_5(self):
        appt = self.make_appt()
        for bad in (0, 6, -1):
            res = self.client.post(url_review(appt.id), {"rating": bad}, format="json")
            self.assertEqual(res.status_code, 400, f"rating={bad}")
        self.assertEqual(self.refresh_counselor(), (0.0, 0))

    def test_one_review_per_appointment(self):
        appt = self.make_appt()
        self.client.post(url_review(appt.id), {"rating": 5}, format="json")
        self.client.post(url_review(appt.id), {"rating": 3}, format="json")
        self.assertEqual(SessionReview.objects.filter(appointment=appt).count(), 1)

    # ── the public list ─────────────────────────────────────────────────

    def test_public_review_list_abbreviates_student_names(self):
        """This list is readable by anyone browsing the directory, so it
        must not publish a student's full name."""
        appt = self.make_appt()
        self.client.post(url_review(appt.id), {"rating": 5, "comment": "Great"},
                         format="json")
        self.client.force_authenticate(None)
        res = self.client.get(f"/api/counseling/counselors/{self.counselor.id}/reviews/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["rating_count"], 1)
        name = res.data["results"][0]["student_name"]
        self.assertEqual(name, "Aditya M.")
        self.assertNotIn("Mehra", name)

    def test_public_stats_now_reports_a_real_average(self):
        """PublicStatsView was structurally incapable of a non-zero rating."""
        appt = self.make_appt()
        self.client.post(url_review(appt.id), {"rating": 4}, format="json")
        self.client.force_authenticate(None)
        res = self.client.get("/api/counseling/stats/")
        self.assertEqual(res.data["avg_rating"], 4.0)


class RescheduleTests(ReviewTestBase):
    def setUp(self):
        self.client.force_authenticate(self.student)
        self.future = timezone.now() + timedelta(days=3)

    def test_reschedule_moves_the_session_and_reports_the_old_time(self):
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED, when=self.future)
        new_at = self.future + timedelta(days=1)
        res = self.client.patch(url_resched(appt.id),
                                {"scheduled_at": new_at.isoformat()}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        appt.refresh_from_db()
        self.assertEqual(appt.scheduled_at, new_at)
        self.assertIsNotNone(res.data["previous_scheduled_at"])

    def test_cannot_reschedule_into_the_past(self):
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED, when=self.future)
        res = self.client.patch(
            url_resched(appt.id),
            {"scheduled_at": (timezone.now() - timedelta(days=1)).isoformat()},
            format="json")
        self.assertEqual(res.status_code, 400)

    def test_cannot_reschedule_onto_a_taken_slot(self):
        """Otherwise reschedule is a way around the availability rules."""
        taken = self.future + timedelta(days=2)
        Appointment.objects.create(
            learner_profile=self.learner, booked_by=self.student,
            counselor=self.counselor, scheduled_at=taken,
            status=Appointment.STATUS_CONFIRMED)
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED, when=self.future)
        res = self.client.patch(url_resched(appt.id),
                                {"scheduled_at": taken.isoformat()}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_cannot_reschedule_a_completed_session(self):
        appt = self.make_appt(status=Appointment.STATUS_COMPLETED)
        res = self.client.patch(
            url_resched(appt.id),
            {"scheduled_at": (self.future).isoformat()}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_cannot_reschedule_someone_elses_appointment(self):
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED, when=self.future)
        self.client.force_authenticate(self.other)
        res = self.client.patch(
            url_resched(appt.id),
            {"scheduled_at": (self.future + timedelta(days=1)).isoformat()},
            format="json")
        self.assertEqual(res.status_code, 404)

    def test_scheduled_at_is_required(self):
        appt = self.make_appt(status=Appointment.STATUS_CONFIRMED, when=self.future)
        res = self.client.patch(url_resched(appt.id), {}, format="json")
        self.assertEqual(res.status_code, 400)


class BookingApprovalTests(ReviewTestBase):
    """Workflow §11 "Requests" — the approve/decline queue.

    The capability is gated by
    GlobalSettings.counselling_booking_requires_approval, which is OFF by
    default. These tests cover both sides of that flag, because the default
    must keep today's instant-confirmation behaviour exactly.
    """

    def setUp(self):
        self.client.force_authenticate(self.cuser)
        self.future = timezone.now() + timedelta(days=5)

    def _settings(self, **kw):
        from global_settings.models import GlobalSettings
        gs = GlobalSettings.objects.first() or GlobalSettings.objects.create()
        for k, v in kw.items():
            setattr(gs, k, v)
        gs.save()
        return gs

    def _pending(self):
        return Appointment.objects.create(
            learner_profile=self.learner, booked_by=self.student,
            counselor=self.counselor, scheduled_at=self.future,
            status=Appointment.STATUS_PENDING, student_note="Stream advice",
        )

    def url(self, a, action):
        return f"/api/counseling/counselor/appointments/{a}/{action}/request/"

    # ── the flag ────────────────────────────────────────────────────────

    def test_bookings_are_confirmed_instantly_by_default(self):
        """The default must not change what students experience today."""
        from counseling.views import _booking_needs_approval
        self.assertFalse(_booking_needs_approval())

    def test_flag_on_makes_bookings_need_approval(self):
        from counseling.views import _booking_needs_approval
        self._settings(counselling_booking_requires_approval=True)
        self.assertTrue(_booking_needs_approval())

    def test_missing_settings_row_does_not_enable_approval(self):
        """A fresh install must not silently queue bookings nobody watches."""
        from global_settings.models import GlobalSettings
        from counseling.views import _booking_needs_approval
        GlobalSettings.objects.all().delete()
        self.assertFalse(_booking_needs_approval())

    # ── approve / decline ───────────────────────────────────────────────

    def test_approve_confirms_the_booking(self):
        appt = self._pending()
        res = self.client.post(self.url(appt.id, "approve"), {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.STATUS_CONFIRMED)

    def test_decline_requires_a_reason(self):
        appt = self._pending()
        res = self.client.post(self.url(appt.id, "decline"), {}, format="json")
        self.assertEqual(res.status_code, 400)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.STATUS_PENDING)

    def test_decline_with_a_reason_rejects_and_stores_it(self):
        appt = self._pending()
        res = self.client.post(self.url(appt.id, "decline"),
                               {"reason": "I am on leave that week."}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.STATUS_REJECTED)
        self.assertIn("on leave", appt.cancel_reason)

    def test_cannot_action_an_already_confirmed_booking(self):
        appt = self._pending()
        appt.status = Appointment.STATUS_CONFIRMED
        appt.save(update_fields=["status"])
        res = self.client.post(self.url(appt.id, "approve"), {}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_approve_409s_when_the_slot_went_while_queued(self):
        """A request sits in a queue; the counsellor may have booked over it."""
        appt = self._pending()
        Appointment.objects.create(
            learner_profile=self.learner, booked_by=self.student,
            counselor=self.counselor, scheduled_at=self.future,
            status=Appointment.STATUS_CONFIRMED,
        )
        res = self.client.post(self.url(appt.id, "approve"), {}, format="json")
        self.assertEqual(res.status_code, 409)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.STATUS_PENDING)

    def test_a_counsellor_cannot_action_another_counsellors_request(self):
        other_user = User.objects.create_user(
            email="rv-other-c@example.com", username="rvotherc", password="x" * 14)
        other = CounselorProfile.objects.create(
            user=other_user, display_name="Dr. Someone Else",
            status=CounselorProfile.STATUS_APPROVED, is_listed=True)
        appt = Appointment.objects.create(
            learner_profile=self.learner, booked_by=self.student,
            counselor=other, scheduled_at=self.future,
            status=Appointment.STATUS_PENDING)
        res = self.client.post(self.url(appt.id, "approve"), {}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_pending_requests_are_listable(self):
        self._pending()
        res = self.client.get("/api/counseling/counselor/appointments/",
                              {"status": "pending"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 1)
