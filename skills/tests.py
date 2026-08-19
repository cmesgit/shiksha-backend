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

    def test_student_decline_reschedule_keeps_original_slot_and_status(self):
        # WORKFLOW.md §3: "Keep original time → reverts to previous status" —
        # declining must NOT cancel the session or free its still-good
        # original slot. Constructed directly (bypassing the propose
        # endpoint), so status_before_reschedule is blank and the view falls
        # back to CONFIRMED, matching the make_session default below.
        session = self.make_session(
            status=SkillSession.STATUS_NEEDS_RECONFIRMATION,
            proposed_slot_key="1-2",
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/decline-reschedule/")
        self.assertEqual(r.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_CONFIRMED)
        self.assertEqual(session.proposed_slot_key, "")
        self.expert.refresh_from_db()
        self.assertIn("0-1", self.expert.availability_slots.get("booked", []))

    def test_student_decline_reschedule_reverts_to_requested_when_proposed_before_acceptance(self):
        session = self.make_session(status=SkillSession.STATUS_REQUESTED)
        client_teacher = self.client_for(self.teacher_user)
        client_teacher.post(
            f"/api/skill/teacher/sessions/{session.id}/reschedule/",
            {"slot_key": "1-2"},
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/decline-reschedule/")
        self.assertEqual(r.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_REQUESTED)
        self.assertEqual(session.status_before_reschedule, "")

    def test_cannot_confirm_reschedule_when_not_awaiting_one(self):
        session = self.make_session(status=SkillSession.STATUS_CONFIRMED)
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/confirm-reschedule/")
        self.assertEqual(r.status_code, 400)


class SkillDevRedesignBackendTests(TestCase):
    """Mastery / no-show / blackout-dates — the new backend surfaces added
    for the Skill Dev redesign (WORKFLOW.md §1, §4, §6)."""

    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher_user = User.objects.create_user(
            username="expert4", email="expert4@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.teacher_user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.teacher_user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile,
            headline="Chess coach",
            is_listed=True,
            mastery_target=2,
            availability_slots={"open": ["0-1"], "booked": ["0-1"]},
        )

        cls.student_user = User.objects.create_user(
            username="learner4", email="learner4@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.student_user, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.learner_profile = LearnerProfile.objects.create(
            account=cls.student_user, display_name="Test Learner 4",
            first_name="Test", last_name="Learner4", is_default=True,
        )

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    def make_session(self, **overrides):
        defaults = dict(
            expert=self.expert, learner_profile=self.learner_profile,
            contact_mode="session", status=SkillSession.STATUS_CONFIRMED,
        )
        defaults.update(overrides)
        return SkillSession.objects.create(**defaults)

    def test_mastery_progress_is_derived_and_updates_with_target(self):
        from .models import mastery_progress

        self.make_session(status=SkillSession.STATUS_COMPLETED)
        m = mastery_progress(self.expert, self.learner_profile)
        self.assertEqual(m, {"progress": 1, "target": 2, "mastered": False})

        self.make_session(status=SkillSession.STATUS_COMPLETED)
        m = mastery_progress(self.expert, self.learner_profile)
        self.assertTrue(m["mastered"])

        # Changing the target re-derives immediately — nothing is snapshotted.
        self.expert.mastery_target = 3
        self.expert.save(update_fields=["mastery_target"])
        m = mastery_progress(self.expert, self.learner_profile)
        self.assertFalse(m["mastered"])

    def test_teacher_can_set_mastery_target_within_bounds(self):
        client = self.client_for(self.teacher_user)
        r = client.put("/api/skill/teacher/mastery-target/", {"target": 5}, format="json")
        self.assertEqual(r.status_code, 200)
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.mastery_target, 5)

        r = client.put("/api/skill/teacher/mastery-target/", {"target": 13}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_teacher_students_roster_reflects_progress(self):
        self.make_session(status=SkillSession.STATUS_COMPLETED)
        client = self.client_for(self.teacher_user)
        r = client.get("/api/skill/teacher/students/")
        self.assertEqual(r.status_code, 200)
        rows = r.data["students"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["progress"], 1)
        self.assertEqual(rows[0]["target"], 2)
        self.assertFalse(rows[0]["mastered"])

    def test_report_no_show_forfeits_session_and_frees_slot(self):
        from django.utils import timezone

        session = self.make_session(
            slot_key="0-1", scheduled_for=timezone.now() - timezone.timedelta(hours=1),
        )
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/report-no-show/")
        self.assertEqual(r.status_code, 200)

        session.refresh_from_db()
        self.assertTrue(session.no_show)
        self.assertEqual(session.status, SkillSession.STATUS_COMPLETED)
        self.expert.refresh_from_db()
        self.assertNotIn("0-1", self.expert.availability_slots.get("booked", []))

    def test_cannot_report_no_show_before_scheduled_time(self):
        from django.utils import timezone

        session = self.make_session(
            scheduled_for=timezone.now() + timezone.timedelta(hours=1),
        )
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/report-no-show/")
        self.assertEqual(r.status_code, 400)

    def test_blackout_date_closes_an_otherwise_open_slot(self):
        from .teacher_views import slot_is_open
        from .views import _slot_to_datetime

        occurs_on = _slot_to_datetime("0-1").date()
        self.expert.availability_slots = {"open": ["0-1"], "booked": []}
        self.expert.save(update_fields=["availability_slots"])
        self.assertTrue(slot_is_open(self.expert, "0-1"))

        client = self.client_for(self.teacher_user)
        r = client.post("/api/skill/teacher/blackouts/", {
            "date_from": str(occurs_on), "date_to": str(occurs_on), "label": "Trip",
        }, format="json")
        self.assertEqual(r.status_code, 201)

        self.assertFalse(slot_is_open(self.expert, "0-1"))

    def test_pricing_ladder_shows_intro_tier_even_when_free_launch_is_on(self):
        # The live design prototype shows the "First session is ₹99" panel
        # even while free-launch is on — only the displayed price is
        # overridden to "Free during launch", not the tier itself.
        from global_settings.models import GlobalSettings
        self.assertTrue(GlobalSettings.load().free_trial_enabled)  # default True

        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.get(f"/api/skill/teachers/{self.expert.id}/pricing/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["is_free"])
        self.assertEqual(r.data["tier"], "intro")

    def test_pricing_ladder_computes_bundle_when_billing_is_on(self):
        from global_settings.models import GlobalSettings

        gs = GlobalSettings.load()
        gs.free_trial_enabled = False
        gs.save(update_fields=["free_trial_enabled"])

        self.make_session(status=SkillSession.STATUS_COMPLETED)  # progress=1, target=2
        self.expert.hourly_rate = 10000
        self.expert.save(update_fields=["hourly_rate"])

        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.get(f"/api/skill/teachers/{self.expert.id}/pricing/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["is_free"])
        self.assertEqual(r.data["tier"], "single")  # remaining == 1, not > 1
        self.assertEqual(r.data["unit_price"], 10000)

        gs.free_trial_enabled = True
        gs.save(update_fields=["free_trial_enabled"])

    def test_review_edit_sets_is_edited_and_recomputes_expert_rating(self):
        from .review_models import ExpertReview

        session = self.make_session(status=SkillSession.STATUS_COMPLETED)
        review = ExpertReview.objects.create(
            session=session, expert=self.expert, learner_profile=self.learner_profile, rating=3,
        )
        self.assertFalse(review.is_edited)

        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.patch(f"/api/skill/my-reviews/{review.id}/", {"rating": 5}, format="json")
        self.assertEqual(r.status_code, 200)
        review.refresh_from_db()
        self.assertTrue(review.is_edited)
        self.expert.refresh_from_db()
        self.assertEqual(float(self.expert.rating), 5.0)

    def test_review_delete_is_permanent_and_recomputes_expert_rating(self):
        from .review_models import ExpertReview

        s1 = self.make_session(status=SkillSession.STATUS_COMPLETED)
        s2 = self.make_session(status=SkillSession.STATUS_COMPLETED)
        r1 = ExpertReview.objects.create(session=s1, expert=self.expert, learner_profile=self.learner_profile, rating=2)
        ExpertReview.objects.create(session=s2, expert=self.expert, learner_profile=self.learner_profile, rating=4)
        self.expert.rating = 3
        self.expert.save(update_fields=["rating"])

        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.delete(f"/api/skill/my-reviews/{r1.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(ExpertReview.objects.filter(id=r1.id).exists())
        self.expert.refresh_from_db()
        self.assertEqual(float(self.expert.rating), 4.0)

    def test_teacher_can_set_a_private_session_note(self):
        session = self.make_session()
        client = self.client_for(self.teacher_user)
        r = client.put(f"/api/skill/teacher/sessions/{session.id}/note/", {"note": "Prefers examples over theory"}, format="json")
        self.assertEqual(r.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.teacher_note, "Prefers examples over theory")

        # Never visible to the student's own session card.
        student_client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r2 = student_client.get(f"/api/skill/sessions/{session.id}/")
        self.assertNotIn("teacher_note", r2.data)

    def test_mark_student_session_complete_bumps_mastery_progress(self):
        session = self.make_session(status=SkillSession.STATUS_CONFIRMED)
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/students/{self.learner_profile.id}/mark-complete/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["progress"], 1)
        session.refresh_from_db()
        self.assertEqual(session.status, SkillSession.STATUS_COMPLETED)

    def test_mark_student_session_complete_requires_a_confirmed_session(self):
        client = self.client_for(self.teacher_user)
        r = client.post(f"/api/skill/teacher/students/{self.learner_profile.id}/mark-complete/")
        self.assertEqual(r.status_code, 400)

    def test_teacher_can_delete_a_blackout(self):
        from .blackout_models import ExpertBlackoutDate
        import datetime

        b = ExpertBlackoutDate.objects.create(
            expert=self.expert, date_from=datetime.date.today(), date_to=datetime.date.today(),
        )
        client = self.client_for(self.teacher_user)
        r = client.delete(f"/api/skill/teacher/blackouts/{b.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(ExpertBlackoutDate.objects.filter(id=b.id).exists())


# =====================================================
# SKILL BROWSE REDESIGN — directory filters, multi-skill listings, ratings
# =====================================================

class SkillBrowseRedesignTests(TestCase):
    """The redesigned public directory + SkillListing CRUD + rating rules."""

    @classmethod
    def setUpTestData(cls):
        from .models import SkillCategory
        from .listing_models import SkillListing

        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")
        cls.music = SkillCategory.objects.create(slug="music", label="Music")
        cls.trades = SkillCategory.objects.create(slug="trades", label="Trades")

        def make_expert(username, headline, **kw):
            user = User.objects.create_user(
                username=username, email=f"{username}@test.com", password="testpass123",
                first_name=headline.split()[0],
            )
            UserRole.objects.create(user=user, role=cls.teacher_role, is_active=True, is_primary=True)
            tp = TeacherProfile.objects.create(user=user, teacher_type=TeacherProfile.TYPE_GUEST)
            defaults = dict(
                teacher_profile=tp, headline=headline, is_listed=True,
                category=cls.music, hourly_rate=50000,
            )
            defaults.update(kw)
            return user, ExpertProfile.objects.create(**defaults)

        cls.guitar_user, cls.guitar = make_expert(
            "guitarist", "Guitar and music theory",
            skill_tags=["Guitar", "Music theory"], languages=["Mizo", "English"],
            experience_years=8, class_mode=ExpertProfile.MODE_ONLINE,
            availability_slots={"open": ["0-1", "1-2"], "booked": ["0-1"]},
        )
        cls.welder_user, cls.welder = make_expert(
            "welder", "Arc welding", category=cls.trades,
            skill_tags=["Welding"], languages=["Mizo"], hourly_rate=90000,
            experience_years=2, class_mode=ExpertProfile.MODE_HOME,
            district="Champhai", pincode="796321",
        )
        SkillListing.objects.create(
            expert=cls.guitar, category=cls.music, title="Beginner guitar",
            skill_tags=["Guitar"], price_paise=40000, order=0,
        )
        SkillListing.objects.create(
            expert=cls.guitar, category=cls.music, title="Church accompaniment",
            skill_tags=["Piano"], price_paise=70000, order=1,
        )

        cls.student_user = User.objects.create_user(
            username="browselearner", email="browselearner@test.com", password="testpass123",
        )
        UserRole.objects.create(user=cls.student_user, role=cls.student_role, is_active=True, is_primary=True)
        cls.learner_profile = LearnerProfile.objects.create(
            account=cls.student_user, display_name="Browse Learner",
            first_name="Browse", last_name="Learner", is_default=True,
        )

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    # ── directory ──────────────────────────────────────────────────────
    def test_directory_is_paginated_and_public(self):
        r = APIClient().get("/api/skill/teachers/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 2)
        self.assertIn("results", r.data)

    def test_search_matches_skill_tags_not_just_headline(self):
        """The bug this redesign fixes: ?search= used to match `headline` only,
        so a learner typing a skill the teacher tags but never headlines got
        nothing back."""
        r = APIClient().get("/api/skill/teachers/", {"search": "Welding"})
        self.assertEqual([row["id"] for row in r.data["results"]], [str(self.welder.id)])

    def test_search_matches_a_listing_title(self):
        r = APIClient().get("/api/skill/teachers/", {"search": "accompaniment"})
        self.assertEqual([row["id"] for row in r.data["results"]], [str(self.guitar.id)])

    def test_search_matches_the_teacher_name(self):
        r = APIClient().get("/api/skill/teachers/", {"search": "Arc"})
        self.assertEqual(r.data["count"], 1)

    def test_district_filter_still_includes_online_teachers(self):
        """Excluding online teachers from a district filter is the commonest
        way a directory says 'no results' to a learner who had options."""
        r = APIClient().get("/api/skill/teachers/", {"district": "Champhai"})
        ids = {row["id"] for row in r.data["results"]}
        self.assertEqual(ids, {str(self.welder.id), str(self.guitar.id)})

    def test_price_max_matches_the_cheapest_listing_not_only_the_expert_rate(self):
        # Guitar's own hourly_rate is ₹500 but its cheapest listing is ₹400.
        r = APIClient().get("/api/skill/teachers/", {"price_max": 450})
        self.assertEqual([row["id"] for row in r.data["results"]], [str(self.guitar.id)])

    def test_language_and_experience_filters(self):
        r = APIClient().get("/api/skill/teachers/", {"lang": "English"})
        self.assertEqual(r.data["count"], 1)
        r = APIClient().get("/api/skill/teachers/", {"min_experience": 5})
        self.assertEqual(r.data["count"], 1)

    def test_mode_filter(self):
        r = APIClient().get("/api/skill/teachers/", {"mode": "online"})
        self.assertEqual([row["id"] for row in r.data["results"]], [str(self.guitar.id)])

    def test_available_week_uses_the_json_grid(self):
        r = APIClient().get("/api/skill/teachers/", {"available_week": 1})
        # Guitar has 2 open, 1 booked → 1 free. Welder has no grid at all.
        self.assertEqual([row["id"] for row in r.data["results"]], [str(self.guitar.id)])

    def test_min_rating_excludes_low_sample_experts(self):
        """A 5.0 built on one review must not survive ?min_rating=4.8 — that is
        the whole point of MIN_REVIEWS."""
        self.guitar.rating = 5
        self.guitar.save(update_fields=["rating"])
        r = APIClient().get("/api/skill/teachers/", {"min_rating": 4.8})
        self.assertEqual(r.data["count"], 0)

    def test_every_sort_option_returns_200(self):
        for sort in ("recommended", "rating", "price_asc", "price_desc",
                     "sessions", "experience", "newest"):
            with self.subTest(sort=sort):
                r = APIClient().get("/api/skill/teachers/", {"sort": sort})
                self.assertEqual(r.status_code, 200, sort)

    def test_row_carries_listings_and_a_from_price(self):
        r = APIClient().get("/api/skill/teachers/", {"search": "Guitar"})
        row = r.data["results"][0]
        self.assertEqual(len(row["listings"]), 2)
        self.assertEqual(row["from_rate"], 400)
        self.assertEqual(row["open_slots_week"], 1)

    def test_directory_stats(self):
        r = APIClient().get("/api/skill/directory-stats/")
        self.assertEqual(r.data["experts"], 2)
        self.assertEqual(r.data["offline"], 1)

    # ── teacher listing CRUD ───────────────────────────────────────────
    def test_teacher_can_create_and_pause_a_listing(self):
        client = self.client_for(self.welder_user)
        r = client.post("/api/skill/teacher/listings/", {
            "title": "Gate fabrication", "category": str(self.trades.id),
            "price_rupees": 800, "description": "Hands-on.", "skill_tags": ["Welding"],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["price_rupees"], 800)

        listing_id = r.data["id"]
        r2 = client.patch(f"/api/skill/teacher/listings/{listing_id}/",
                          {"is_active": False}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.data["is_active"])

    def test_a_teacher_cannot_touch_another_teachers_listing(self):
        listing = self.guitar.listings.first()
        r = self.client_for(self.welder_user).get(f"/api/skill/teacher/listings/{listing.id}/")
        self.assertEqual(r.status_code, 404)

    def test_deleting_a_listing_with_sessions_is_refused(self):
        listing = self.guitar.listings.first()
        SkillSession.objects.create(
            expert=self.guitar, learner_profile=self.learner_profile,
            listing=listing, status=SkillSession.STATUS_COMPLETED,
        )
        r = self.client_for(self.guitar_user).delete(f"/api/skill/teacher/listings/{listing.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.guitar.listings.filter(id=listing.id).exists())

    def test_a_suspended_listing_cannot_be_re_listed_by_its_teacher(self):
        listing = self.guitar.listings.first()
        listing.is_suspended = True
        listing.is_active = False
        listing.save(update_fields=["is_suspended", "is_active"])
        r = self.client_for(self.guitar_user).patch(
            f"/api/skill/teacher/listings/{listing.id}/", {"is_active": True}, format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_a_fourth_listing_in_a_week_raises_one_moderation_flag(self):
        from .listing_models import ListingModerationFlag

        client = self.client_for(self.welder_user)
        for i in range(5):
            r = client.post("/api/skill/teacher/listings/", {
                "title": f"Trade skill {i}", "category": str(self.trades.id),
                "price_rupees": 500, "description": "x",
            }, format="json")
            self.assertEqual(r.status_code, 201, r.data)
        # Flagged from the 4th onward, deduplicated into a single open item.
        self.assertEqual(
            ListingModerationFlag.objects.filter(expert=self.welder, is_open=True).count(), 1
        )

    # ── reviews ────────────────────────────────────────────────────────
    def test_public_reviews_send_date_edited_flag_and_distribution(self):
        listing = self.guitar.listings.first()
        session = SkillSession.objects.create(
            expert=self.guitar, learner_profile=self.learner_profile, listing=listing,
            status=SkillSession.STATUS_COMPLETED, note="Barre chords",
        )
        from .review_models import ExpertReview
        ExpertReview.objects.create(
            session=session, expert=self.guitar, learner_profile=self.learner_profile,
            rating=4, body="Patient teacher.",
        )
        r = APIClient().get(f"/api/skill/teachers/{self.guitar.id}/reviews/")
        review = r.data["reviews"][0]
        self.assertIn("created_at", review)
        self.assertIn("is_edited", review)
        self.assertEqual(review["topic"], "Barre chords")
        self.assertEqual(review["listing"], str(listing.id))
        self.assertEqual(r.data["distribution"]["4"], 1)
        # One review is not an average — it is withheld until MIN_REVIEWS.
        self.assertIsNone(r.data["average"])

    def test_submitting_a_review_updates_both_cached_averages(self):
        listing = self.guitar.listings.first()
        session = SkillSession.objects.create(
            expert=self.guitar, learner_profile=self.learner_profile, listing=listing,
            status=SkillSession.STATUS_COMPLETED,
        )
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post(f"/api/skill/sessions/{session.id}/review/", {"rating": 5}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.guitar.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(float(self.guitar.rating), 5.0)
        self.assertEqual(float(listing.rating), 5.0)
        self.assertEqual(listing.sessions_count, 1)

    # ── booking is per-skill ────────────────────────────────────────────
    def test_booking_a_specific_listing_prices_and_records_that_skill(self):
        listing = self.guitar.listings.get(title="Church accompaniment")
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post("/api/skill/payments/create-order/", {
            "teacherId": str(self.guitar.id), "listing": str(listing.id),
            "draft": {"slot": "1-2"},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        # ₹700 from the listing, NOT the profile's legacy ₹500 hourly_rate.
        self.assertEqual(r.data["amount_rupees"], 700)
        self.assertEqual(r.data["listing"], str(listing.id))
        self.assertEqual(
            SkillSession.objects.get(id=r.data["sessionId"]).listing_id, listing.id
        )

    def test_booking_without_a_listing_falls_back_to_the_primary_one(self):
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post("/api/skill/payments/create-order/", {
            "teacherId": str(self.guitar.id), "draft": {"slot": "1-2"},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["listing_title"], "Beginner guitar")

    def test_a_paused_skill_cannot_be_booked(self):
        listing = self.guitar.listings.get(title="Beginner guitar")
        listing.is_active = False
        listing.save(update_fields=["is_active"])
        client = self.client_for(self.student_user, active_profile=self.learner_profile)
        r = client.post("/api/skill/payments/create-order/", {
            "teacherId": str(self.guitar.id), "listing": str(listing.id),
            "draft": {"slot": "1-2"},
        }, format="json")
        self.assertEqual(r.status_code, 400)

    # ── category counts ────────────────────────────────────────────────
    def test_category_list_counts_each_expert_once(self):
        """The M2M mirrors the primary category on every listing write, so a
        naive `Count(experts) + Count(multi_experts)` would double them."""
        self.guitar.categories.add(self.music, self.trades)
        r = APIClient().get("/api/skill/categories/")
        counts = {row["slug"]: row["expert_count"] for row in r.data}
        self.assertEqual(counts["music"], 1)    # guitar only
        self.assertEqual(counts["trades"], 2)   # welder (primary) + guitar (M2M)

    def test_search_does_not_match_a_paused_listing(self):
        """A paused skill surfacing in search sends a learner to a teacher for
        something they can't book."""
        listing = self.guitar.listings.get(title="Church accompaniment")
        listing.is_active = False
        listing.save(update_fields=["is_active"])
        r = APIClient().get("/api/skill/teachers/", {"search": "accompaniment"})
        self.assertEqual(r.data["count"], 0)


# ══════════════════════════════════════════════════════════════════════════
# Durable notifications for skill events
# ══════════════════════════════════════════════════════════════════════════

class SkillSessionNotificationTests(TestCase):
    """Skill Dev used to emit ONLY an Activity row + a live WS frame, so a
    learner who wasn't staring at the tab was never told their session had
    been confirmed or declined — no email, no push, no bell history. These
    lock in the durable Notification row, its recipient, and its deep link.

    The link assertions matter as much as the row: learner links point at the
    student dashboard (/skill-dev/sessions/<id>) and expert links at the
    teacher app (/teacher/expert/...) — two different apps, so a link built
    for the wrong side is a dead click, not a cosmetic bug.
    """

    LEARNER_APP = "/skill-dev/sessions/"
    EXPERT_APP = "/teacher/expert/bookings"

    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.expert_user = User.objects.create_user(
            username="notif_expert", email="notif_expert@test.com",
            password="testpass123",
        )
        UserRole.objects.create(user=cls.expert_user, role=cls.teacher_role,
                                is_active=True, is_primary=True)
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.expert_user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile,
            headline="Guitar teacher", is_listed=True,
        )

        cls.learner_user = User.objects.create_user(
            username="notif_learner", email="notif_learner@test.com",
            password="testpass123",
        )
        UserRole.objects.create(user=cls.learner_user, role=cls.student_role,
                                is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.learner_user, display_name="Nina Learner",
            first_name="Nina", last_name="Learner", is_default=True,
        )

    def setUp(self):
        # Email/SMS/push dispatch is a real network call for the REQUIRED-level
        # skill verbs. Stub the three side-effect helpers so these tests assert
        # the persisted row (the thing that was actually missing) without
        # depending on Resend/MSG91/FCM credentials.
        for target in ("_dispatch_email", "_dispatch_sms", "_dispatch_push",
                       "_push_ws"):
            patcher = patch(f"notifications.services.{target}")
            patcher.start()
            self.addCleanup(patcher.stop)

    def client_for(self, user, active_profile=None):
        client = APIClient()
        token = {"active_profile": str(active_profile.id)} if active_profile else None
        client.force_authenticate(user=user, token=token)
        return client

    def only_notification(self, verb):
        from notifications.models import Notification
        qs = Notification.objects.filter(verb=verb)
        self.assertEqual(qs.count(), 1, f"expected exactly one {verb} row")
        return qs.get()

    # ── booking request → the EXPERT ──────────────────────────────────
    def test_session_request_notifies_the_expert(self):
        client = self.client_for(self.learner_user, self.learner)
        r = client.post("/api/skill/sessions/", {
            "expert": self.expert.id, "contact_mode": "session",
        })
        self.assertEqual(r.status_code, 201)

        n = self.only_notification("skill.requested")
        self.assertEqual(n.recipient, self.expert_user)
        self.assertEqual(n.actor, self.learner_user)
        self.assertEqual(n.link_url, self.EXPERT_APP)
        self.assertEqual(n.audience_identity, f"T:{self.teacher_profile.id}")
        self.assertEqual(n.audience_role, "TEACHER")
        self.assertEqual(n.payload["session_id"], r.data["sessionId"])

    # ── confirmation → the LEARNER ────────────────────────────────────
    def test_confirmation_notifies_the_learner_with_the_student_app_link(self):
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner,
            contact_mode="session", status=SkillSession.STATUS_REQUESTED,
        )
        client = self.client_for(self.expert_user)
        r = client.post(f"/api/skill/teacher/sessions/{session.id}/confirm/")
        self.assertEqual(r.status_code, 200)

        n = self.only_notification("skill.confirmed")
        self.assertEqual(n.recipient, self.learner_user)
        self.assertEqual(n.actor, self.expert_user)
        self.assertEqual(n.link_url, f"{self.LEARNER_APP}{session.id}")
        # Per-profile scope: a sibling on the same account must not see it.
        self.assertEqual(n.audience_identity, f"L:{self.learner.id}")
        self.assertEqual(n.audience_role, "STUDENT")

    # ── cancellation → the OTHER party, never the actor ───────────────
    def test_learner_cancellation_notifies_the_expert_not_the_learner(self):
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner,
            contact_mode="session", status=SkillSession.STATUS_REQUESTED,
        )
        client = self.client_for(self.learner_user, self.learner)
        r = client.post(f"/api/skill/sessions/{session.id}/cancel/")
        self.assertEqual(r.status_code, 200)

        n = self.only_notification("skill.cancelled")
        self.assertEqual(n.recipient, self.expert_user)
        self.assertEqual(n.actor, self.learner_user)
        self.assertEqual(n.link_url, self.EXPERT_APP)

        from notifications.models import Notification
        self.assertFalse(
            Notification.objects.filter(recipient=self.learner_user).exists(),
            "the person who cancelled must not be notified of their own action",
        )

    # ── the Activity/WS layer is untouched ────────────────────────────
    def test_activity_row_still_written_alongside_the_notification(self):
        """Notifications are additive — the live bell still runs off Activity."""
        from activity.models import Activity
        session = SkillSession.objects.create(
            expert=self.expert, learner_profile=self.learner,
            contact_mode="session", status=SkillSession.STATUS_REQUESTED,
        )
        client = self.client_for(self.expert_user)
        client.post(f"/api/skill/teacher/sessions/{session.id}/confirm/")

        self.assertTrue(
            Activity.objects.filter(user=self.learner_user,
                                    object_id=session.id).exists()
        )
        self.only_notification("skill.confirmed")

    # ── every emitted verb is registered ──────────────────────────────
    def test_every_skill_verb_is_registered_in_policy(self):
        """An unregistered verb falls through to policy._DEFAULT — silently
        no email, and invisible to the user's preference toggles."""
        from notifications import policy
        from .notifications import _NOTIFY_SPEC
        for event, (verb, _t, _b) in _NOTIFY_SPEC.items():
            self.assertIn(verb, policy.POLICY, f"{event} -> {verb} unregistered")
            self.assertIn(policy.POLICY[verb]["category"], policy.CATEGORIES)
