"""The cross-session notes list stays private and counselor scoped."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import LearnerProfile
from counseling.models import Appointment, CounselorProfile, SessionNote


class CounselorNotesIndexTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.student = User.objects.create_user(
            email="notes-student@example.com", username="notesstudent", password="password123")
        learner = LearnerProfile.objects.create(
            account=self.student, display_name="Notes Student", is_default=True)
        self.owner = User.objects.create_user(
            email="notes-owner@example.com", username="notesowner", password="password123")
        self.other = User.objects.create_user(
            email="notes-other@example.com", username="notesother", password="password123")
        self.counselor = CounselorProfile.objects.create(
            user=self.owner, display_name="Owner", status=CounselorProfile.STATUS_APPROVED)
        other_counselor = CounselorProfile.objects.create(
            user=self.other, display_name="Other", status=CounselorProfile.STATUS_APPROVED)
        def appointment(counselor, status):
            return Appointment.objects.create(
                learner_profile=learner, booked_by=self.student, counselor=counselor,
                scheduled_at=timezone.now() - timedelta(days=1), status=status)
        self.completed = appointment(self.counselor, Appointment.STATUS_COMPLETED)
        self.pending_note = appointment(self.counselor, Appointment.STATUS_COMPLETED)
        self.confirmed = appointment(self.counselor, Appointment.STATUS_CONFIRMED)
        stranger = appointment(other_counselor, Appointment.STATUS_COMPLETED)
        SessionNote.objects.create(appointment=self.completed, counselor=self.counselor,
                                   content="Private observation")
        SessionNote.objects.create(appointment=stranger, counselor=other_counselor,
                                   content="Other counselor secret")

    def test_only_own_completed_sessions_and_notes_are_returned(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get("/api/counseling/counselor/notes/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["appointment_id"]: row for row in response.data["results"]}
        self.assertEqual(set(rows), {self.completed.id, self.pending_note.id})
        self.assertEqual(rows[self.completed.id]["note_count"], 1)
        self.assertEqual(rows[self.completed.id]["latest_note"]["content"], "Private observation")
        self.assertEqual(rows[self.pending_note.id]["note_count"], 0)
        self.assertIsNone(rows[self.pending_note.id]["latest_note"])

    def test_student_cannot_read_private_notes_index(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.get("/api/counseling/counselor/notes/").status_code, 403)
