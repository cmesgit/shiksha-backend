"""
Tests for sessions_app — Private Sessions feature.

Covers:
  - Model creation & constraints
  - Permissions (IsTeacher, IsStudent)
  - Full session lifecycle (request → approve → start → end)
  - Reschedule flow (propose → confirm / decline)
  - Cancel flow
  - Decline flow
  - Session detail access control
  - Join (LiveKit token) endpoint
  - Edge cases (wrong status transitions, unauthorized access)
"""

from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, Profile, Role, UserRole
from .models import PrivateSession, SessionParticipant, SessionRescheduleHistory


# ===================================================================
# HELPERS
# ===================================================================

class BaseTestCase(TestCase):
    """Sets up a teacher, a student, roles, and profiles for every test."""

    @classmethod
    def setUpTestData(cls):
        # --- Roles ---
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        # --- Teacher ---
        cls.teacher = User.objects.create_user(
            username="teacher1",
            email="teacher@test.com",
            password="testpass123",
        )
        Profile.objects.create(
            user=cls.teacher,
            full_name="Test Teacher",
            phone="1111111111",
        )
        UserRole.objects.create(
            user=cls.teacher,
            role=cls.teacher_role,
            is_active=True,
            is_primary=True,
        )

        # --- Student ---
        cls.student = User.objects.create_user(
            username="student1",
            email="student@test.com",
            password="testpass123",
        )
        Profile.objects.create(
            user=cls.student,
            full_name="Test Student",
            phone="2222222222",
            student_id="STU001",
        )
        UserRole.objects.create(
            user=cls.student,
            role=cls.student_role,
            is_active=True,
            is_primary=True,
        )

        # --- Second student (for group session tests) ---
        cls.student2 = User.objects.create_user(
            username="student2",
            email="student2@test.com",
            password="testpass123",
        )
        Profile.objects.create(
            user=cls.student2,
            full_name="Second Student",
            phone="3333333333",
            student_id="STU002",
        )
        UserRole.objects.create(
            user=cls.student2,
            role=cls.student_role,
            is_active=True,
            is_primary=True,
        )

        # --- Unrelated user (no role) ---
        cls.outsider = User.objects.create_user(
            username="outsider",
            email="outsider@test.com",
            password="testpass123",
        )

    def get_client(self, user):
        """Return an authenticated APIClient for the given user."""
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def create_session(self, **overrides):
        """Shortcut to create a PrivateSession with sensible defaults."""
        defaults = {
            "teacher": self.teacher,
            "requested_by": self.student,
            "subject": "Mathematics",
            "scheduled_date": date.today() + timedelta(days=1),
            "scheduled_time": time(14, 0),
            "duration_minutes": 60,
            "session_type": "one_on_one",
            "group_strength": 1,
            "status": "pending",
        }
        defaults.update(overrides)
        session = PrivateSession.objects.create(**defaults)
        SessionParticipant.objects.create(
            session=session, user=defaults["requested_by"], role="student"
        )
        return session


# ===================================================================
# MODEL TESTS
# ===================================================================

class PrivateSessionModelTest(BaseTestCase):

    def test_create_session(self):
        session = self.create_session()
        self.assertEqual(session.status, "pending")
        self.assertEqual(session.subject, "Mathematics")
        self.assertEqual(session.teacher, self.teacher)
        self.assertEqual(session.requested_by, self.student)

    def test_session_str(self):
        session = self.create_session()
        self.assertIn("Mathematics", str(session))
        self.assertIn("pending", str(session))

    def test_participant_unique_together(self):
        session = self.create_session()
        # First participant created in create_session()
        with self.assertRaises(Exception):
            SessionParticipant.objects.create(
                session=session, user=self.student, role="student"
            )

    def test_reschedule_history_created(self):
        session = self.create_session()
        history = SessionRescheduleHistory.objects.create(
            session=session,
            proposed_by=self.teacher,
            original_date=session.scheduled_date,
            original_time=session.scheduled_time,
            proposed_date=date.today() + timedelta(days=3),
            proposed_time=time(16, 0),
            reason="Conflict",
        )
        self.assertEqual(session.reschedule_history.count(), 1)
        self.assertIn("Reschedule", str(history))


# ===================================================================
# PERMISSION TESTS
# ===================================================================

class PermissionTest(BaseTestCase):

    def test_student_cannot_access_teacher_endpoints(self):
        client = self.get_client(self.student)
        res = client.get("/api/private-sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_teacher_cannot_access_student_endpoints(self):
        client = self.get_client(self.teacher)
        res = client.get("/api/private-sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_rejected(self):
        client = APIClient()
        res = client.get("/api/private-sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_role_user_rejected(self):
        client = self.get_client(self.outsider)
        res = client.get("/api/private-sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = client.get("/api/private-sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# REQUEST SESSION TESTS (Student → POST /request/)
# ===================================================================

class RequestSessionTest(BaseTestCase):

    def test_student_can_request_session(self):
        client = self.get_client(self.student)
        res = client.post("/api/private-sessions/request/", {
            "teacher_id": str(self.teacher.id),
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
            "duration_minutes": 45,
            "session_type": "one_on_one",
            "group_strength": 1,
            "notes": "Help with chapter 5",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["status"], "pending")
        self.assertEqual(res.data["subject"], "Physics")
        self.assertEqual(res.data["teacher_name"], "Test Teacher")
        self.assertEqual(res.data["student_name"], "Test Student")

    def test_request_creates_participant(self):
        client = self.get_client(self.student)
        res = client.post("/api/private-sessions/request/", {
            "teacher_id": str(self.teacher.id),
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
        }, format="json")
        session_id = res.data["id"]
        session = PrivateSession.objects.get(pk=session_id)
        self.assertEqual(session.participants.count(), 1)
        self.assertEqual(session.participants.first().user, self.student)

    def test_request_with_group_students(self):
        client = self.get_client(self.student)
        res = client.post("/api/private-sessions/request/", {
            "teacher_id": str(self.teacher.id),
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
            "session_type": "group",
            "group_strength": 2,
            "student_ids": ["STU002"],
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        session = PrivateSession.objects.get(pk=res.data["id"])
        self.assertEqual(session.participants.count(), 2)

    def test_request_invalid_teacher(self):
        client = self.get_client(self.student)
        res = client.post("/api/private-sessions/request/", {
            "teacher_id": str(self.student.id),  # student, not teacher
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_teacher_cannot_request_session(self):
        client = self.get_client(self.teacher)
        res = client.post("/api/private-sessions/request/", {
            "teacher_id": str(self.teacher.id),
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# STUDENT SESSION LIST TESTS
# ===================================================================

class StudentSessionListTest(BaseTestCase):

    def test_scheduled_tab(self):
        self.create_session(status="approved")
        self.create_session(status="ongoing")
        self.create_session(status="completed")  # should NOT appear
        client = self.get_client(self.student)
        res = client.get("/api/private-sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 2)

    def test_requests_tab(self):
        self.create_session(status="pending")
        self.create_session(status="approved")  # should NOT appear
        client = self.get_client(self.student)
        res = client.get("/api/private-sessions/student/?tab=requests")
        self.assertEqual(len(res.data), 1)

    def test_history_tab(self):
        self.create_session(status="completed")
        self.create_session(status="cancelled")
        self.create_session(status="declined")
        self.create_session(status="pending")  # should NOT appear
        client = self.get_client(self.student)
        res = client.get("/api/private-sessions/student/?tab=history")
        self.assertEqual(len(res.data), 3)

    def test_student_only_sees_own_sessions(self):
        self.create_session(requested_by=self.student2, status="pending")
        client = self.get_client(self.student)
        res = client.get("/api/private-sessions/student/?tab=requests")
        self.assertEqual(len(res.data), 0)


# ===================================================================
# TEACHER ACTION TESTS (accept, decline, reschedule)
# ===================================================================

class TeacherActionTest(BaseTestCase):

    def test_accept_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/accept/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], "approved")

    def test_accept_with_time_override(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        new_date = str(date.today() + timedelta(days=5))
        res = client.post(f"/api/private-sessions/{session.id}/accept/", {
            "scheduled_date": new_date,
            "scheduled_time": "16:00",
        }, format="json")
        self.assertEqual(res.data["scheduled_date"], new_date)

    def test_cannot_accept_non_pending(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/accept/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_decline_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/decline/", {
            "reason": "Not available"
        }, format="json")
        self.assertEqual(res.data["status"], "declined")
        self.assertEqual(res.data["decline_reason"], "Not available")

    def test_reschedule_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        new_date = str(date.today() + timedelta(days=4))
        res = client.post(f"/api/private-sessions/{session.id}/reschedule/", {
            "scheduled_date": new_date,
            "scheduled_time": "15:00",
            "reason": "Have a meeting",
        }, format="json")
        self.assertEqual(res.data["status"], "needs_reconfirmation")
        self.assertEqual(res.data["rescheduled_date"], new_date)
        # Audit trail created
        self.assertEqual(SessionRescheduleHistory.objects.filter(session=session).count(), 1)

    def test_reschedule_missing_fields(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/reschedule/", {
            "reason": "No date provided"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wrong_teacher_cannot_act(self):
        """A teacher who isn't assigned to this session can't accept it."""
        other_teacher = User.objects.create_user(
            username="teacher2", email="teacher2@test.com", password="testpass123"
        )
        Profile.objects.create(user=other_teacher, full_name="Other Teacher")
        UserRole.objects.create(
            user=other_teacher, role=self.teacher_role, is_active=True, is_primary=True
        )
        session = self.create_session(status="pending")
        client = self.get_client(other_teacher)
        res = client.post(f"/api/private-sessions/{session.id}/accept/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


# ===================================================================
# TEACHER SESSION LIST TESTS
# ===================================================================

class TeacherSessionListTest(BaseTestCase):

    def test_teacher_sessions(self):
        self.create_session(status="approved")
        self.create_session(status="ongoing")
        self.create_session(status="pending")  # should NOT appear
        client = self.get_client(self.teacher)
        res = client.get("/api/private-sessions/teacher/sessions/")
        self.assertEqual(len(res.data), 2)

    def test_teacher_requests(self):
        self.create_session(status="pending")
        self.create_session(status="approved")  # should NOT appear
        client = self.get_client(self.teacher)
        res = client.get("/api/private-sessions/teacher/requests/")
        self.assertEqual(len(res.data), 1)

    def test_teacher_history(self):
        self.create_session(status="completed")
        self.create_session(status="cancelled")
        self.create_session(status="ongoing")  # should NOT appear
        client = self.get_client(self.teacher)
        res = client.get("/api/private-sessions/teacher/history/")
        self.assertEqual(len(res.data), 2)


# ===================================================================
# RESCHEDULE CONFIRM / DECLINE (Student)
# ===================================================================

class RescheduleResponseTest(BaseTestCase):

    def test_confirm_reschedule(self):
        session = self.create_session(
            status="needs_reconfirmation",
            rescheduled_date=date.today() + timedelta(days=5),
            rescheduled_time=time(16, 0),
        )
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(res.data["status"], "approved")
        self.assertEqual(res.data["scheduled_date"], str(date.today() + timedelta(days=5)))
        self.assertIsNone(res.data["rescheduled_date"])

    def test_decline_reschedule(self):
        session = self.create_session(status="needs_reconfirmation")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/decline-reschedule/")
        self.assertEqual(res.data["status"], "declined")

    def test_confirm_wrong_status(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ===================================================================
# CANCEL TESTS
# ===================================================================

class CancelSessionTest(BaseTestCase):

    def test_student_cancel_pending(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/cancel/", {
            "reason": "Changed my mind"
        }, format="json")
        self.assertEqual(res.data["status"], "cancelled")
        self.assertEqual(res.data["cancel_reason"], "Changed my mind")

    def test_student_cancel_approved(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/cancel/")
        self.assertEqual(res.data["status"], "cancelled")

    def test_cannot_cancel_completed(self):
        session = self.create_session(status="completed")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/cancel/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_cancel_ongoing(self):
        session = self.create_session(status="ongoing")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/cancel/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ===================================================================
# SESSION LIFECYCLE (start → end)
# ===================================================================

class SessionLifecycleTest(BaseTestCase):

    def test_start_session(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/start/")
        self.assertEqual(res.data["status"], "ongoing")
        self.assertTrue(res.data["room_name"].startswith("private-"))
        self.assertIsNotNone(res.data["started_at"])

    def test_cannot_start_pending(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/start/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_session(self):
        session = self.create_session(
            status="ongoing",
            room_name="private-test",
            started_at=timezone.now(),
        )
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/end/")
        self.assertEqual(res.data["status"], "completed")
        self.assertIsNotNone(res.data["ended_at"])

    def test_cannot_end_approved(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/end/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_student_cannot_start_session(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/start/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# SESSION DETAIL & ACCESS CONTROL
# ===================================================================

class SessionDetailTest(BaseTestCase):

    def test_teacher_can_view(self):
        session = self.create_session()
        client = self.get_client(self.teacher)
        res = client.get(f"/api/private-sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["id"], str(session.id))

    def test_student_can_view(self):
        session = self.create_session()
        client = self.get_client(self.student)
        res = client.get(f"/api/private-sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_participant_can_view(self):
        session = self.create_session()
        SessionParticipant.objects.create(
            session=session, user=self.student2, role="student"
        )
        client = self.get_client(self.student2)
        res = client.get(f"/api/private-sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_outsider_cannot_view(self):
        session = self.create_session()
        client = self.get_client(self.outsider)
        res = client.get(f"/api/private-sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_detail_includes_participants(self):
        session = self.create_session()
        client = self.get_client(self.teacher)
        res = client.get(f"/api/private-sessions/{session.id}/")
        self.assertIn("participants", res.data)
        self.assertEqual(len(res.data["participants"]), 1)


# ===================================================================
# JOIN SESSION (LiveKit token)
# ===================================================================

class JoinPrivateSessionTest(BaseTestCase):

    @patch("sessions_app.views.generate_livekit_token")
    def test_teacher_can_join(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["token"], "fake-jwt-token")
        self.assertEqual(res.data["room"], "private-test-room")
        self.assertEqual(res.data["role"], "TEACHER")
        mock_token.assert_called_once_with(
            user=self.teacher,
            session=session,
            is_teacher=True,
        )

    @patch("sessions_app.views.generate_livekit_token")
    def test_student_can_join(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.student)
        res = client.post(f"/api/private-sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["role"], "STUDENT")

    @patch("sessions_app.views.generate_livekit_token")
    def test_join_tracks_participant_time(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.student)
        client.post(f"/api/private-sessions/{session.id}/join/")
        participant = SessionParticipant.objects.get(session=session, user=self.student)
        self.assertIsNotNone(participant.joined_at)

    def test_cannot_join_non_ongoing(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outsider_cannot_join(self):
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.outsider)
        res = client.post(f"/api/private-sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_join_without_room(self):
        session = self.create_session(status="ongoing", room_name="")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/private-sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ===================================================================
# SERIALIZER HELPER TESTS
# ===================================================================

class SerializerHelperTest(BaseTestCase):

    def test_get_user_name_with_profile(self):
        from .serializers import get_user_name
        self.assertEqual(get_user_name(self.teacher), "Test Teacher")
        self.assertEqual(get_user_name(self.student), "Test Student")

    def test_get_user_name_without_profile(self):
        from .serializers import get_user_name
        self.assertEqual(get_user_name(self.outsider), self.outsider.username)

    def test_get_user_name_none(self):
        from .serializers import get_user_name
        self.assertEqual(get_user_name(None), "Unknown")

    def test_get_student_id(self):
        from .serializers import get_student_id
        self.assertEqual(get_student_id(self.student), "STU001")
        self.assertIsNone(get_student_id(self.teacher))
        self.assertIsNone(get_student_id(self.outsider))