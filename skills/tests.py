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
