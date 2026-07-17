"""
Tests for the expert intro-video feature (skills app).

Covers:
  - create → save flow sets intro_video_status on the caller's own profile
  - the pre-booking feed (StudentSkillExpertsView) and post-booking feed
    (SkillSessionDetailView) both surface intro_video_embed_url once ready
"""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, TeacherProfile, LearnerProfile
from .models import ExpertProfile, SkillSession


class IntroVideoFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher_user = User.objects.create_user(
            username="expert1", email="expert1@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.teacher_user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.teacher_user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile,
            headline="Guitar teacher",
            is_listed=True,
        )

        cls.student_user = User.objects.create_user(
            username="learner1", email="learner1@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.student_user, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.learner_profile = LearnerProfile.objects.create(
            account=cls.student_user, display_name="Test Learner",
            first_name="Test", last_name="Learner", is_default=True,
        )
        cls.session = SkillSession.objects.create(
            expert=cls.expert, learner_profile=cls.learner_profile,
            contact_mode="session",
        )

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    @patch("skills.views_intro_video.requests.post")
    def test_create_and_save_intro_video(self, mock_post):
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {"guid": "abc123"}

        client = self.client_for(self.teacher_user)
        r = client.post("/api/skill/teacher/intro-video/create/", {"title": "intro"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["video_id"], "abc123")

        r2 = client.post("/api/skill/teacher/intro-video/save/", {"video_id": "abc123"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["intro_video_status"], 1)
        # Not "Finished" yet, so no playable embed URL should be surfaced.
        self.assertIsNone(r2.data["intro_video_embed_url"])

        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_bunny_id, "abc123")

    def test_intro_video_surfaces_on_student_experts_and_session_detail(self):
        self.expert.intro_video_bunny_id = "readyvid"
        self.expert.intro_video_status = 4  # Finished
        self.expert.save(update_fields=["intro_video_bunny_id", "intro_video_status"])

        client = self.client_for(self.student_user, active_profile=self.learner_profile)

        r = client.get("/api/skill/student/experts/")
        self.assertEqual(r.status_code, 200)
        row = next(e for e in r.data if e["id"] == str(self.expert.id))
        self.assertTrue(row["intro_video_embed_url"].endswith("/readyvid"))

        r2 = client.get(f"/api/skill/sessions/{self.session.id}/")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data["expert"]["intro_video_embed_url"].endswith("/readyvid"))

    def test_intro_video_absent_when_not_finished(self):
        client = self.client_for(self.student_user)
        r = client.get("/api/skill/student/experts/")
        row = next(e for e in r.data if e["id"] == str(self.expert.id))
        self.assertIsNone(row["intro_video_embed_url"])


class SkillSessionCancelDeclineTests(TestCase):
    """Covers the two Phase-1 booking-edge-case fixes:
    - the student-cancel endpoint (previously unwired — 404) releases the
      booked slot, matching the teacher-decline path.
    - an expert can no longer decline a session that's already live.
    """

    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher_user = User.objects.create_user(
            username="expert2", email="expert2@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.teacher_user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.teacher_user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile,
            headline="Piano teacher",
            is_listed=True,
            availability_slots={"open": ["0-1"], "booked": ["0-1"]},
        )

        cls.student_user = User.objects.create_user(
            username="learner2", email="learner2@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.student_user, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.learner_profile = LearnerProfile.objects.create(
            account=cls.student_user, display_name="Test Learner 2",
            first_name="Test", last_name="Learner2", is_default=True,
        )

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    def test_student_cancel_requested_session_frees_slot(self):
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_REQUESTED,
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/cancel/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], SkillSession.STATUS_CANCELLED)

        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CANCELLED)
        self.expert.refresh_from_db()
        self.assertNotIn("0-1", self.expert.availability_slots.get("booked", []))

    def test_student_cannot_cancel_confirmed_session(self):
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_CONFIRMED,
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/cancel/")
        self.assertEqual(r.status_code, 409)
        # Slot stays booked — cancel was rejected.
        self.expert.refresh_from_db()
        self.assertIn("0-1", self.expert.availability_slots.get("booked", []))

    def test_teacher_can_decline_confirmed_session_not_yet_started(self):
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_CONFIRMED,
        )
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/decline/")
        self.assertEqual(r.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CANCELLED)

    def test_teacher_cannot_decline_a_live_session(self):
        from django.utils import timezone

        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_CONFIRMED,
            started_at=timezone.now(),
        )
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/decline/")
        self.assertEqual(r.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CONFIRMED)

    def test_decline_creates_a_bell_notification_for_the_student(self):
        from activity.models import Activity

        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_REQUESTED,
        )
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/decline/")
        self.assertEqual(r.status_code, 200)

        activity = Activity.objects.filter(
            user=self.student_user, type=Activity.TYPE_SESSION, object_id=session.id,
        ).first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.learner_profile_id, self.learner_profile.id)


class SkillSessionRescheduleTests(TestCase):
    """Covers the Phase-2 reschedule flow: expert proposes a new slot,
    learner confirms or declines."""

    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher_user = User.objects.create_user(
            username="expert3", email="expert3@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.teacher_user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.teacher_user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile,
            headline="Violin teacher",
            is_listed=True,
            availability_slots={"open": ["0-1", "1-2"], "booked": ["0-1"]},
        )

        cls.student_user = User.objects.create_user(
            username="learner3", email="learner3@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.student_user, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.learner_profile = LearnerProfile.objects.create(
            account=cls.student_user, display_name="Test Learner 3",
            first_name="Test", last_name="Learner3", is_default=True,
        )

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    def make_session(self, **overrides):
        defaults = dict(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", slot_key="0-1",
            status=SkillSession.STATUS_CONFIRMED,
        )
        defaults.update(overrides)
        return SkillSession.objects.create(**defaults)

    def test_teacher_can_propose_a_reschedule(self):
        session = self.make_session()
        client = self.client_for(self.teacher_user)
        r = client.post(
            f"/api/skill/teacher/sessions/{session.id}/reschedule/",
            {"slot_key": "1-2", "reason": "Traffic"},
        )
        self.assertEqual(r.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_NEEDS_RECONFIRMATION)
        self.assertEqual(session.proposed_slot_key, "1-2")
        self.assertIsNotNone(session.proposed_scheduled_for)

    def test_teacher_cannot_propose_a_slot_that_isnt_open(self):
        session = self.make_session()
        client = self.client_for(self.teacher_user)
        r = client.post(
            f"/api/skill/teacher/sessions/{session.id}/reschedule/",
            {"slot_key": "5-5"},  # never marked open
        )
        self.assertEqual(r.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CONFIRMED)

    def test_teacher_cannot_reschedule_a_live_session(self):
        from django.utils import timezone

        session = self.make_session(started_at=timezone.now())
        client = self.client_for(self.teacher_user)
        r = client.post(
            f"/api/skill/teacher/sessions/{session.id}/reschedule/",
            {"slot_key": "1-2"},
        )
        self.assertEqual(r.status_code, 400)

    def test_student_confirm_reschedule_swaps_the_slot(self):
        session = self.make_session(
            status=SkillSession.STATUS_NEEDS_RECONFIRMATION,
            proposed_slot_key="1-2",
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(r.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CONFIRMED)
        self.assertEqual(session.slot_key, "1-2")
        self.assertEqual(session.proposed_slot_key, "")

        self.expert.refresh_from_db()
        booked = self.expert.availability_slots.get("booked", [])
        self.assertIn("1-2", booked)
        self.assertNotIn("0-1", booked)

    def test_student_decline_reschedule_cancels_and_frees_the_old_slot(self):
        session = self.make_session(
            status=SkillSession.STATUS_NEEDS_RECONFIRMATION,
            proposed_slot_key="1-2",
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/decline-reschedule/")
        self.assertEqual(r.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CANCELLED)
        self.expert.refresh_from_db()
        self.assertNotIn("0-1", self.expert.availability_slots.get("booked", []))

    def test_cannot_confirm_reschedule_when_not_awaiting_one(self):
        session = self.make_session(status=SkillSession.STATUS_CONFIRMED)
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(r.status_code, 400)
