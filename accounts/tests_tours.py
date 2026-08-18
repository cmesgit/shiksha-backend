"""
Tests for the product-tour endpoints in accounts/settings_views.py
(TOUR_SYSTEM_SPEC.md §4.2 / TOUR_BUILD_GUIDE.md phase 1).

Run with:
    DJANGO_SETTINGS_MODULE=config.settings_test .venv/bin/python manage.py test \
        accounts.tests_tours -v2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, TeacherProfile, TourState, User


class TourEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="tanvi", email="tanvi@example.com", password="whatever-9",
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.user, display_name="Tanvi", relationship="SELF", is_default=True,
        )
        cls.teacher_user = User.objects.create_user(
            username="rekha", email="rekha@example.com", password="whatever-9",
        )
        cls.teacher_profile = TeacherProfile.objects.create(user=cls.teacher_user)

    def learner_client(self, user=None, profile=None):
        client = APIClient()
        client.force_authenticate(
            user=user or self.user,
            token={"context": "learner", "active_profile": str((profile or self.profile).id)},
        )
        return client

    def teacher_client(self):
        client = APIClient()
        client.force_authenticate(user=self.teacher_user, token={"context": "teacher"})
        return client

    def no_profile_client(self):
        client = APIClient()
        client.force_authenticate(user=self.user, token={"context": "learner"})
        return client

    # ── identity resolution ─────────────────────────────────────────────

    def test_learner_identity_resolves_to_L_prefixed_key(self):
        res = self.learner_client().get("/api/accounts/tours/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["identity_key"], f"L:{self.profile.id}")

    def test_teacher_identity_resolves_to_T_prefixed_key(self):
        res = self.teacher_client().get("/api/accounts/tours/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["identity_key"], f"T:{self.teacher_profile.id}")

    def test_two_profiles_on_one_account_get_independent_state(self):
        other = LearnerProfile.objects.create(
            account=self.user, display_name="Sibling", relationship="DEPENDENT",
        )
        self.learner_client().patch(
            "/api/accounts/tours/",
            {"tour_key": "student.welcome.academy", "status": "completed", "version": 1, "step": 5},
            content_type="application/json",
        )
        res = self.learner_client(profile=other).get("/api/accounts/tours/")
        self.assertEqual(res.json()["tours"], {})
        self.assertEqual(TourState.objects.count(), 2)

    def test_no_profile_selected_returns_409(self):
        res = self.no_profile_client().get("/api/accounts/tours/")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "profile_required")

    def test_no_context_at_all_returns_409(self):
        client = APIClient()
        client.force_authenticate(user=self.user, token={})
        res = client.get("/api/accounts/tours/")
        self.assertEqual(res.status_code, 409, res.content)

    # ── PATCH merge semantics ────────────────────────────────────────────

    def test_patch_merges_into_tours_map_without_clobbering_other_keys(self):
        client = self.learner_client()
        client.patch(
            "/api/accounts/tours/",
            {"tour_key": "student.welcome.academy", "status": "completed", "version": 1, "step": 5},
            content_type="application/json",
        )
        res = client.patch(
            "/api/accounts/tours/",
            {"tour_key": "student.courses.detail", "status": "dismissed", "version": 1, "step": 1},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        tours = res.json()["tours"]
        self.assertEqual(tours["student.welcome.academy"]["status"], "completed")
        self.assertEqual(tours["student.courses.detail"]["status"], "dismissed")

    def test_patch_rejects_unknown_status(self):
        res = self.learner_client().patch(
            "/api/accounts/tours/",
            {"tour_key": "student.welcome.academy", "status": "in_progress"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_autoplay_enabled_alone(self):
        client = self.learner_client()
        res = client.patch(
            "/api/accounts/tours/", {"autoplay_enabled": False}, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["autoplay_enabled"])

    def test_patch_with_neither_field_is_rejected(self):
        res = self.learner_client().patch(
            "/api/accounts/tours/", {}, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    # ── R5: three consecutive dismissals ────────────────────────────────

    def test_three_consecutive_dismissals_disables_autoplay(self):
        client = self.learner_client()
        for key in ("a", "b", "c"):
            res = client.patch(
                "/api/accounts/tours/",
                {"tour_key": key, "status": "dismissed", "version": 1, "step": 1},
                content_type="application/json",
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["consecutive_dismissals"], 3)
        self.assertFalse(res.json()["autoplay_enabled"])

    def test_a_completion_resets_the_dismissal_counter(self):
        client = self.learner_client()
        for key in ("a", "b"):
            client.patch(
                "/api/accounts/tours/",
                {"tour_key": key, "status": "dismissed", "version": 1, "step": 1},
                content_type="application/json",
            )
        res = client.patch(
            "/api/accounts/tours/",
            {"tour_key": "c", "status": "completed", "version": 1, "step": 4},
            content_type="application/json",
        )
        self.assertEqual(res.json()["consecutive_dismissals"], 0)
        self.assertTrue(res.json()["autoplay_enabled"])

    # ── absence / once-per-UTC-day rotation ─────────────────────────────

    def test_first_ever_get_sets_last_seen_and_flags_first_session(self):
        res = self.learner_client().get("/api/accounts/tours/")
        body = res.json()
        self.assertTrue(body["is_first_session"])
        self.assertEqual(body["absence_days"], 0)
        state = TourState.objects.get(identity_key=f"L:{self.profile.id}")
        self.assertIsNotNone(state.last_seen_at)
        self.assertIsNone(state.prev_seen_at)

    def test_same_day_second_get_does_not_rotate(self):
        client = self.learner_client()
        client.get("/api/accounts/tours/")
        state = TourState.objects.get(identity_key=f"L:{self.profile.id}")
        first_seen = state.last_seen_at

        res = client.get("/api/accounts/tours/")
        self.assertFalse(res.json()["is_first_session"])
        state.refresh_from_db()
        self.assertEqual(state.last_seen_at, first_seen)
        self.assertIsNone(state.prev_seen_at)

    def test_a_later_day_get_rotates_last_seen_into_prev_seen(self):
        state = TourState.for_identity(f"L:{self.profile.id}", self.user)
        old_seen = timezone.now() - timedelta(days=50)
        state.last_seen_at = old_seen
        state.save(update_fields=["last_seen_at"])

        res = self.learner_client().get("/api/accounts/tours/")
        body = res.json()
        self.assertFalse(body["is_first_session"])
        self.assertGreaterEqual(body["absence_days"], 49)

        state.refresh_from_db()
        self.assertEqual(state.prev_seen_at, old_seen)
        self.assertGreater(state.last_seen_at, old_seen)

    # ── reset ────────────────────────────────────────────────────────────

    def test_reset_one_tour(self):
        client = self.learner_client()
        client.patch(
            "/api/accounts/tours/",
            {"tour_key": "a", "status": "completed", "version": 1, "step": 1},
            content_type="application/json",
        )
        client.patch(
            "/api/accounts/tours/",
            {"tour_key": "b", "status": "completed", "version": 1, "step": 1},
            content_type="application/json",
        )
        res = client.post(
            "/api/accounts/tours/reset/", {"tour_key": "a"}, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("a", res.json()["tours"])
        self.assertIn("b", res.json()["tours"])

    def test_reset_all_tours(self):
        client = self.learner_client()
        client.patch(
            "/api/accounts/tours/",
            {"tour_key": "a", "status": "completed", "version": 1, "step": 1},
            content_type="application/json",
        )
        res = client.post(
            "/api/accounts/tours/reset/", {"all": True}, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["tours"], {})

    def test_reset_requires_tour_key_or_all(self):
        res = self.learner_client().post(
            "/api/accounts/tours/reset/", {}, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    # ── admin kill switch ────────────────────────────────────────────────

    def test_tours_enabled_feature_flag_surfaces_in_response(self):
        from global_settings.models import GlobalSettings

        gs = GlobalSettings.load()
        gs.tours_enabled = False
        gs.save(update_fields=["tours_enabled"])

        res = self.learner_client().get("/api/accounts/tours/")
        self.assertFalse(res.json()["features"]["tours_enabled"])
