"""AppointmentSerializer.get_learner — the context the counsellor console needs.

design_handoff_admin_counsellor, counsellor Phase 3.

The artboard renders "Class 12 · Science (PCB)" and "Class 10 · Booked by a
parent" under each booking request. Neither could be built before: the
serializer returned only {id, display_name}, so the frontend had nothing to
render and the line was correctly left out rather than faked.

These pin the two facts that line depends on.
"""
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from accounts.models import LearnerProfile, User
from counseling.models import Appointment, CounselorProfile
from counseling.serializers import AppointmentSerializer


class LearnerContextTest(TestCase):
    def setUp(self):
        self.counsellor_user = User.objects.create_user(
            username="c_ctx", email="c_ctx@e.com", password="x")
        self.counsellor = CounselorProfile.objects.create(
            user=self.counsellor_user, status=CounselorProfile.STATUS_APPROVED,
            display_name="Dr. Ctx")

    def _learner(self, username, **profile):
        u = User.objects.create_user(
            username=username, email=f"{username}@e.com", password="x")
        lp = u.learner_profiles.first() or LearnerProfile.objects.create(
            account=u, display_name=username)
        for k, v in profile.items():
            setattr(lp, k, v)
        lp.display_name = profile.get("display_name", lp.display_name)
        lp.save()
        return u, lp

    def _appt(self, lp, booked_by):
        return Appointment.objects.create(
            counselor=self.counsellor, learner_profile=lp, booked_by=booked_by,
            scheduled_at=timezone.now() + timedelta(days=2),
            duration_minutes=45, status=Appointment.STATUS_CONFIRMED)

    def test_class_stream_and_board_are_exposed(self):
        u, lp = self._learner(
            "ctx1", current_class="12", stream="Science (PCB)", board="CBSE")
        data = AppointmentSerializer(self._appt(lp, u)).data["learner"]
        self.assertEqual(data["current_class"], "12")
        self.assertEqual(data["stream"], "Science (PCB)")
        self.assertEqual(data["board"], "CBSE")

    def test_missing_context_is_empty_string_not_null(self):
        """The UI joins these with ' · '. Nulls would render as "null"."""
        u, lp = self._learner("ctx2")
        data = AppointmentSerializer(self._appt(lp, u)).data["learner"]
        for key in ("current_class", "stream", "board"):
            self.assertEqual(data[key], "", f"{key} should be '' when unset")

    def test_booking_for_yourself_is_not_flagged(self):
        u, lp = self._learner("ctx3")
        data = AppointmentSerializer(self._appt(lp, booked_by=u)).data["learner"]
        self.assertFalse(data["booked_by_other"])

    def test_a_parent_booking_for_a_child_IS_flagged(self):
        """⚠ THE POINT. booked_by is the account holder; learner_profile is
        who the session is for. A parent booking for a dependent makes those
        two different accounts — that difference IS the signal."""
        parent = User.objects.create_user(
            username="parent1", email="parent1@e.com", password="x")
        _, child = self._learner("child1", current_class="10")
        data = AppointmentSerializer(self._appt(child, booked_by=parent)).data["learner"]
        self.assertTrue(data["booked_by_other"])

    def test_display_name_and_id_still_present(self):
        """The two fields that existed before must not regress — every
        counsellor screen reads them."""
        u, lp = self._learner("ctx4")
        data = AppointmentSerializer(self._appt(lp, u)).data["learner"]
        self.assertEqual(data["display_name"], lp.display_name)
        self.assertEqual(data["id"], str(lp.id))
