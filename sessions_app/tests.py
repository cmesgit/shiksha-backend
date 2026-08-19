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

from accounts.models import User, Role, UserRole, LearnerProfile
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
            first_name="Test",
            last_name="Teacher",
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
        LearnerProfile.objects.create(
            account=cls.student, display_name="Test Student",
            full_name="Test Student", student_id="STU001", is_default=True,
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
        LearnerProfile.objects.create(
            account=cls.student2, display_name="Second Student",
            full_name="Second Student", student_id="STU002", is_default=True,
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

    def get_client(self, user, profile=None, context=None):
        """Return an authenticated APIClient carrying a JWT-like context token.

        Student endpoints resolve the active learner profile from the token
        (`get_active_profile`) and teacher endpoints require `context=="teacher"`
        (`IsTeacherContext`), the same way `build_tokens` does in production. By
        default the context is inferred from the user's role (teacher → teacher,
        else learner + default profile); override with `context=`/`profile=`.
        """
        client = APIClient()
        if context is None:
            context = "teacher" if user.has_role("TEACHER") else "learner"
        token = {"context": context}
        if context == "learner":
            if profile is None:
                profile = (
                    user.learner_profiles.filter(is_default=True).first()
                    or user.learner_profiles.first()
                )
            if profile is not None:
                token["active_profile"] = str(profile.id)
        client.force_authenticate(user=user, token=token)
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
        res = client.get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_teacher_cannot_access_student_endpoints(self):
        client = self.get_client(self.teacher)
        res = client.get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_rejected(self):
        client = APIClient()
        res = client.get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_role_user_rejected(self):
        client = self.get_client(self.outsider)
        res = client.get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = client.get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# REQUEST SESSION TESTS (Student → POST /request/)
# ===================================================================

class RequestSessionTest(BaseTestCase):

    def test_student_can_request_session(self):
        client = self.get_client(self.student)
        res = client.post("/api/sessions/request/", {
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
        res = client.post("/api/sessions/request/", {
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
        res = client.post("/api/sessions/request/", {
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
        res = client.post("/api/sessions/request/", {
            "teacher_id": str(self.student.id),  # student, not teacher
            "subject": "Physics",
            "scheduled_date": str(date.today() + timedelta(days=2)),
            "scheduled_time": "10:00",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_teacher_cannot_request_session(self):
        client = self.get_client(self.teacher)
        res = client.post("/api/sessions/request/", {
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
        res = client.get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 2)

    def test_requests_tab(self):
        self.create_session(status="pending")
        self.create_session(status="approved")  # should NOT appear
        client = self.get_client(self.student)
        res = client.get("/api/sessions/student/?tab=requests")
        self.assertEqual(len(res.data), 1)

    def test_history_tab(self):
        self.create_session(status="completed")
        self.create_session(status="cancelled")
        self.create_session(status="declined")
        self.create_session(status="pending")  # should NOT appear
        client = self.get_client(self.student)
        res = client.get("/api/sessions/student/?tab=history")
        self.assertEqual(len(res.data), 3)

    def test_student_only_sees_own_sessions(self):
        self.create_session(requested_by=self.student2, status="pending")
        client = self.get_client(self.student)
        res = client.get("/api/sessions/student/?tab=requests")
        self.assertEqual(len(res.data), 0)


# ===================================================================
# TEACHER ACTION TESTS (accept, decline, reschedule)
# ===================================================================

class TeacherActionTest(BaseTestCase):

    def test_accept_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/accept/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], "approved")

    def test_accept_with_time_override(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        new_date = str(date.today() + timedelta(days=5))
        res = client.post(f"/api/sessions/{session.id}/accept/", {
            "scheduled_date": new_date,
            "scheduled_time": "16:00",
        }, format="json")
        self.assertEqual(res.data["scheduled_date"], new_date)

    def test_cannot_accept_non_pending(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/accept/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_decline_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/decline/", {
            "reason": "Not available"
        }, format="json")
        self.assertEqual(res.data["status"], "declined")
        self.assertEqual(res.data["decline_reason"], "Not available")

    def test_reschedule_request(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        new_date = str(date.today() + timedelta(days=4))
        res = client.post(f"/api/sessions/{session.id}/reschedule/", {
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
        res = client.post(f"/api/sessions/{session.id}/reschedule/", {
            "reason": "No date provided"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wrong_teacher_cannot_act(self):
        """A teacher who isn't assigned to this session can't accept it."""
        other_teacher = User.objects.create_user(
            username="teacher2", email="teacher2@test.com", password="testpass123"
        )
        UserRole.objects.create(
            user=other_teacher, role=self.teacher_role, is_active=True, is_primary=True
        )
        session = self.create_session(status="pending")
        client = self.get_client(other_teacher)
        res = client.post(f"/api/sessions/{session.id}/accept/")
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
        res = client.get("/api/sessions/teacher/sessions/")
        self.assertEqual(len(res.data), 2)

    def test_teacher_requests(self):
        self.create_session(status="pending")
        self.create_session(status="approved")  # should NOT appear
        client = self.get_client(self.teacher)
        res = client.get("/api/sessions/teacher/requests/")
        self.assertEqual(len(res.data), 1)

    def test_teacher_history(self):
        self.create_session(status="completed")
        self.create_session(status="cancelled")
        self.create_session(status="ongoing")  # should NOT appear
        client = self.get_client(self.teacher)
        res = client.get("/api/sessions/teacher/history/")
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
        res = client.post(f"/api/sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(res.data["status"], "approved")
        self.assertEqual(res.data["scheduled_date"], str(date.today() + timedelta(days=5)))
        self.assertIsNone(res.data["rescheduled_date"])

    def test_decline_reschedule(self):
        session = self.create_session(status="needs_reconfirmation")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/decline-reschedule/")
        self.assertEqual(res.data["status"], "declined")

    def test_confirm_wrong_status(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ===================================================================
# CANCEL TESTS
# ===================================================================

class CancelSessionTest(BaseTestCase):

    def test_student_cancel_pending(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/cancel/", {
            "reason": "Changed my mind"
        }, format="json")
        self.assertEqual(res.data["status"], "cancelled")
        self.assertEqual(res.data["cancel_reason"], "Changed my mind")

    def test_student_cancel_approved(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/cancel/")
        self.assertEqual(res.data["status"], "cancelled")

    def test_cannot_cancel_completed(self):
        session = self.create_session(status="completed")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/cancel/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_cancel_ongoing(self):
        session = self.create_session(status="ongoing")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/cancel/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ===================================================================
# SESSION LIFECYCLE (start → end)
# ===================================================================

class SessionLifecycleTest(BaseTestCase):

    def test_start_session(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/start/")
        self.assertEqual(res.data["status"], "ongoing")
        self.assertTrue(res.data["room_name"].startswith("private-"))
        self.assertIsNotNone(res.data["started_at"])

    def test_cannot_start_pending(self):
        session = self.create_session(status="pending")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/start/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_session(self):
        session = self.create_session(
            status="ongoing",
            room_name="private-test",
            started_at=timezone.now(),
        )
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/end/")
        self.assertEqual(res.data["status"], "completed")
        self.assertIsNotNone(res.data["ended_at"])

    def test_cannot_end_approved(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/end/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_student_cannot_start_session(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/start/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# SESSION DETAIL & ACCESS CONTROL
# ===================================================================

class SessionDetailTest(BaseTestCase):

    def test_teacher_can_view(self):
        session = self.create_session()
        client = self.get_client(self.teacher)
        res = client.get(f"/api/sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["id"], str(session.id))

    def test_student_can_view(self):
        session = self.create_session()
        client = self.get_client(self.student)
        res = client.get(f"/api/sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_participant_can_view(self):
        session = self.create_session()
        SessionParticipant.objects.create(
            session=session, user=self.student2, role="student"
        )
        client = self.get_client(self.student2)
        res = client.get(f"/api/sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_outsider_cannot_view(self):
        session = self.create_session()
        client = self.get_client(self.outsider)
        res = client.get(f"/api/sessions/{session.id}/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_detail_includes_participants(self):
        session = self.create_session()
        client = self.get_client(self.teacher)
        res = client.get(f"/api/sessions/{session.id}/")
        self.assertIn("participants", res.data)
        self.assertEqual(len(res.data["participants"]), 1)


# ===================================================================
# JOIN SESSION (LiveKit token)
# ===================================================================

class JoinPrivateSessionTest(BaseTestCase):

    @patch("sessions_app.views.generate_private_token")
    def test_teacher_can_join(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["token"], "fake-jwt-token")
        self.assertEqual(res.data["room"], "private-test-room")
        self.assertEqual(res.data["role"], "TEACHER")
        mock_token.assert_called_once()
        call_kwargs = mock_token.call_args.kwargs
        self.assertEqual(call_kwargs["user"], self.teacher)
        self.assertEqual(call_kwargs["session"], session)

    @patch("sessions_app.views.generate_private_token")
    def test_student_can_join(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.student)
        res = client.post(f"/api/sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["role"], "STUDENT")

    @patch("sessions_app.views.generate_private_token")
    def test_join_tracks_participant_time(self, mock_token):
        mock_token.return_value = "fake-jwt-token"
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.student)
        client.post(f"/api/sessions/{session.id}/join/")
        participant = SessionParticipant.objects.get(session=session, user=self.student)
        self.assertIsNotNone(participant.joined_at)

    def test_cannot_join_non_ongoing(self):
        session = self.create_session(status="approved")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outsider_cannot_join(self):
        session = self.create_session(
            status="ongoing",
            room_name="private-test-room",
            started_at=timezone.now(),
        )
        client = self.get_client(self.outsider)
        res = client.post(f"/api/sessions/{session.id}/join/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_join_without_room(self):
        session = self.create_session(status="ongoing", room_name="")
        client = self.get_client(self.teacher)
        res = client.post(f"/api/sessions/{session.id}/join/")
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

# ===================================================================
# MULTI-PROFILE ISOLATION  (two children on ONE account)
# ===================================================================

class ProfileIsolationTest(TestCase):
    """A private session belongs to the booking learner_profile, not the whole
    account, so two children on one account never see or act on each other's
    tutoring sessions."""

    @classmethod
    def setUpTestData(cls):
        cls.student_role = Role.objects.create(name="STUDENT")
        cls.teacher_role = Role.objects.create(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="tara", email="tara@test.com", password="x",
            first_name="Tara", last_name="Teach",
        )
        UserRole.objects.create(
            user=cls.teacher, role=cls.teacher_role,
            is_active=True, is_primary=True,
        )

        # ONE account with TWO children.
        cls.account = User.objects.create_user(
            username="parent", email="parent@test.com", password="x",
        )
        UserRole.objects.create(
            user=cls.account, role=cls.student_role,
            is_active=True, is_primary=True,
        )
        cls.child_a = LearnerProfile.objects.create(
            account=cls.account, display_name="Aria",
            full_name="Aria Kid", is_default=True,
        )
        cls.child_b = LearnerProfile.objects.create(
            account=cls.account, display_name="Bina",
            full_name="Bina Kid", is_default=False,
        )

    def _mk(self, profile, subject="Mathematics", status_="approved"):
        s = PrivateSession.objects.create(
            teacher=self.teacher, requested_by=self.account,
            learner_profile=profile, subject=subject,
            scheduled_date=date.today() + timedelta(days=1),
            scheduled_time=time(14, 0), status=status_,
        )
        SessionParticipant.objects.create(
            session=s, user=self.account, role="student", status="accepted",
        )
        return s

    def _client(self, profile=None, context="learner"):
        c = APIClient()
        token = {"context": context}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        c.force_authenticate(user=self.account, token=token)
        return c

    def test_list_scoped_to_active_profile(self):
        self._mk(self.child_a)
        self._mk(self.child_b)
        res = self._client(self.child_a).get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["learner_profile_id"], str(self.child_a.id))
        self.assertEqual(res.data[0]["student_name"], "Aria Kid")

    def test_sibling_cannot_cancel_others_session(self):
        s = self._mk(self.child_a, status_="pending")
        res = self._client(self.child_b).post(f"/api/sessions/{s.id}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, 404)
        s.refresh_from_db()
        self.assertEqual(s.status, "pending")

    def test_owner_can_cancel_own_session(self):
        s = self._mk(self.child_a, status_="pending")
        res = self._client(self.child_a).post(f"/api/sessions/{s.id}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, 200)
        s.refresh_from_db()
        self.assertEqual(s.status, "cancelled")

    def test_legacy_null_row_shows_only_for_default_profile(self):
        legacy = self._mk(self.child_a)
        legacy.learner_profile = None
        legacy.save(update_fields=["learner_profile"])
        res_a = self._client(self.child_a).get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(len(res_a.data), 1)  # default profile still sees it
        res_b = self._client(self.child_b).get("/api/sessions/student/?tab=scheduled")
        self.assertEqual(len(res_b.data), 0)  # non-default does not

    def test_no_profile_selected_returns_403(self):
        res = self._client(profile=None, context="account").get(
            "/api/sessions/student/?tab=scheduled"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["lock_reason"], "no_learner_profile")


# ===================================================================
# TEACHER-CONTEXT ENFORCEMENT  (outstanding #1)
# ===================================================================

class TeacherContextEnforcementTest(BaseTestCase):
    """Teacher endpoints require BOTH the TEACHER role AND a teacher-context
    token. A learner-context token on a teacher's account (e.g. a child on a
    shared device) must be rejected even though has_role('TEACHER') passes."""

    def _client(self, user, context):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": context})
        return c

    def test_teacher_context_allowed(self):
        res = self._client(self.teacher, "teacher").get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_learner_context_on_teacher_account_blocked(self):
        res = self._client(self.teacher, "learner").get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_account_context_blocked(self):
        res = self._client(self.teacher, "account").get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_token_blocked(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token=None)
        res = c.get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_teacher_in_teacher_context_blocked(self):
        # A student who somehow carries a teacher-context token still lacks the role.
        res = self._client(self.student, "teacher").get("/api/sessions/teacher/sessions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ===================================================================
# GROUP SESSION — UPDATE (edit a still-scheduled session)
# ===================================================================
# No existing GroupSession fixtures in this file (PrivateSession above uses a
# plain CharField `subject`, not a real Subject/Course FK) — self-contained
# setup mirroring livestream/tests.py's Board/Course/Subject/Batch pattern,
# since GroupSession's invite validation genuinely needs those.

class GroupSessionUpdateTest(TestCase):
    def setUp(self):
        from courses.models import Board, Course, Subject
        from enrollments.models import Enrollment
        from .models import GroupSession, GroupSessionInvite

        self.host = User.objects.create_user(
            username="gs_host@x.com", email="gs_host@x.com", password="x"
        )
        teacher_role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(
            user=self.host, role=teacher_role, is_active=True, is_primary=True
        )

        self.student = User.objects.create_user(
            username="gs_student@x.com", email="gs_student@x.com", password="x"
        )
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        Enrollment.objects.create(
            user=self.student, course=self.course, status=Enrollment.STATUS_ACTIVE
        )

        self.not_enrolled = User.objects.create_user(
            username="gs_out@x.com", email="gs_out@x.com", password="x"
        )

        self.now = timezone.now()
        self.session = GroupSession.objects.create(
            host=self.host, subject=self.subject,
            subject_name=self.subject.name, course_title=self.course.title,
            topic="Original topic",
            scheduled_date=(self.now + timedelta(days=1)).date(),
            scheduled_time=time(15, 0),
            duration_minutes=45,
            status="scheduled",
        )
        self.GroupSessionInvite = GroupSessionInvite

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.host, token={"context": "teacher"})
        return client

    def test_host_can_edit_topic_and_time(self):
        r = self._client().patch(
            f"/api/sessions/group-sessions/{self.session.id}/",
            {"topic": "Updated topic", "scheduled_time": "16:30", "duration_minutes": 60},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.session.refresh_from_db()
        self.assertEqual(self.session.topic, "Updated topic")
        self.assertEqual(self.session.duration_minutes, 60)
        self.assertEqual(self.session.scheduled_time, time(16, 30))

    def test_non_host_cannot_edit(self):
        other = User.objects.create_user(
            username="gs_other@x.com", email="gs_other@x.com", password="x"
        )
        UserRole.objects.create(
            user=other, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=True
        )
        r = self._client(user=other).patch(
            f"/api/sessions/group-sessions/{self.session.id}/",
            {"topic": "Hijacked"},
            format="json",
        )
        self.assertEqual(r.status_code, 404)  # scoped query excludes non-hosts, same as cancel

    def test_cannot_edit_once_live(self):
        self.session.status = "live"
        self.session.save(update_fields=["status"])
        r = self._client().patch(
            f"/api/sessions/group-sessions/{self.session.id}/",
            {"topic": "Too late"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_additive_invite_adds_only_new_valid_students(self):
        # Pre-existing invite — must survive an edit that doesn't mention it,
        # and must not be duplicated if resubmitted.
        self.GroupSessionInvite.objects.create(
            session=self.session, user=self.student, invite_role="student"
        )
        r = self._client().patch(
            f"/api/sessions/group-sessions/{self.session.id}/",
            {"invited_user_ids": [str(self.student.id), str(self.not_enrolled.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        invited_ids = set(
            self.session.invites.values_list("user_id", flat=True)
        )
        self.assertEqual(invited_ids, {self.student.id})  # not_enrolled never added, no dupes

    def test_reschedule_to_the_past_rejected(self):
        r = self._client().patch(
            f"/api/sessions/group-sessions/{self.session.id}/",
            {"scheduled_date": (self.now - timedelta(days=1)).date().isoformat()},
            format="json",
        )
        self.assertEqual(r.status_code, 400)


# ===================================================================
# SUBJECT ROSTER — authorization
# ===================================================================
# /api/sessions/subjects/<id>/students/ was gated on IsAuthenticated alone,
# which made it an enumerable roster dump: subject ids are discoverable from
# the catalog, so any logged-in account could pull every enrolled student's
# name and student_id for any course. It cannot simply become teacher-only —
# learners legitimately call it to invite classmates to a group session
# (groupSessionService.getCourseStudents) — so both audiences, and nobody
# else, must pass.

class SubjectStudentsAccessTest(TestCase):
    def setUp(self):
        from courses.models import Board, Course, Subject, TeachingAssignment
        from enrollments.models import Enrollment

        board = Board.objects.create(name="CBSE-R", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C9", class_level=9)
        self.subject = Subject.objects.create(course=self.course, name="Maths")

        teacher_role, _ = Role.objects.get_or_create(name="TEACHER")

        self.teacher = User.objects.create_user(
            username="ros_t@x.com", email="ros_t@x.com", password="x")
        UserRole.objects.create(user=self.teacher, role=teacher_role,
                                is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=self.teacher, is_active=True)

        self.other_teacher = User.objects.create_user(
            username="ros_t2@x.com", email="ros_t2@x.com", password="x")
        UserRole.objects.create(user=self.other_teacher, role=teacher_role,
                                is_active=True, is_primary=True)

        self.classmate = User.objects.create_user(
            username="ros_s@x.com", email="ros_s@x.com", password="x")
        Enrollment.objects.create(user=self.classmate, course=self.course,
                                  status=Enrollment.STATUS_ACTIVE)

        self.peer = User.objects.create_user(
            username="ros_s2@x.com", email="ros_s2@x.com", password="x")
        Enrollment.objects.create(user=self.peer, course=self.course,
                                  status=Enrollment.STATUS_ACTIVE)

        self.outsider = User.objects.create_user(
            username="ros_out@x.com", email="ros_out@x.com", password="x")

        self.url = f"/api/sessions/subjects/{self.subject.id}/students/"

    def _get(self, user, context=None):
        client = APIClient()
        client.force_authenticate(
            user=user, token={"context": context} if context else {})
        return client.get(self.url)

    def test_outsider_cannot_enumerate_the_roster(self):
        """The actual hole: any authenticated non-member could read it."""
        self.assertEqual(self._get(self.outsider).status_code, 403)

    def test_unrelated_teacher_cannot_read_the_roster(self):
        r = self._get(self.other_teacher, context="teacher")
        self.assertEqual(r.status_code, 403)

    def test_assigned_teacher_in_teacher_context_can_read(self):
        r = self._get(self.teacher, context="teacher")
        self.assertEqual(r.status_code, 200)

    def test_enrolled_student_can_read_for_group_invites(self):
        """Must keep working — this is the group-session invite picker."""
        r = self._get(self.classmate)
        self.assertEqual(r.status_code, 200)
        # Sees the peer, never themselves.
        ids = {row["user_id"] for row in r.data}
        self.assertIn(str(self.peer.id), ids)
        self.assertNotIn(str(self.classmate.id), ids)

    # ── the sibling fork: /subjects/<id>/teachers/ ────────────────────────
    def _get_teachers(self, user, context=None):
        client = APIClient()
        client.force_authenticate(
            user=user, token={"context": context} if context else {})
        return client.get(f"/api/sessions/subjects/{self.subject.id}/teachers/")

    def test_outsider_cannot_list_subject_teachers(self):
        """Was the last fork still gated on IsAuthenticated alone."""
        self.assertEqual(self._get_teachers(self.outsider).status_code, 403)

    def test_enrolled_student_can_list_subject_teachers(self):
        """Must keep working — this is the private-session request picker."""
        r = self._get_teachers(self.classmate)
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.teacher.id), {row["id"] for row in r.data})

    def test_assigned_teacher_can_list_subject_teachers(self):
        """Must keep working — host inviting a co-teacher to a group session."""
        self.assertEqual(
            self._get_teachers(self.teacher, context="teacher").status_code, 200)
