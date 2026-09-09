"""The expert dashboard payload must say why an expert is not discoverable.

ExpertProfile.refresh_listing only sets is_listed once completeness() passes,
so a freshly added Skill Dev track is unlisted with a blank profile. The
dashboard's profile_todo used to carry only needs_payment / needs_location,
neither of which answers "why can nobody find me?" — and no frontend read
`missing` anywhere, so the expert was never told.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import TeacherProfile
from skills.models import ExpertProfile

User = get_user_model()


class ExpertDashboardCompletenessTest(TestCase):
    def setUp(self):
        # username is required by this project's UserManager.
        self.user = User.objects.create_user(
            username="expert@example.com", email="expert@example.com",
            password="pw12345678",
        )
        self.tp = TeacherProfile.objects.create(user=self.user)
        self.ep = ExpertProfile.objects.create(teacher_profile=self.tp)

    def test_a_blank_expert_is_reported_incomplete_and_unlisted(self):
        """The exact state a one-click Skill Dev signup lands in."""
        todo = self._todo()
        self.assertFalse(todo["is_complete"])
        self.assertFalse(todo["is_listed"])
        self.assertTrue(todo["missing"], "must name what is missing, not just say 'incomplete'")

    def test_missing_names_the_personal_fields_registration_never_collects(self):
        """Account-first /register collects only email + password + terms, and
        the personal half of completeness reads the SELF learner profile — so a
        brand-new account is missing its own name."""
        todo = self._todo()
        for key in ("full_name", "date_of_birth", "phone", "profile_photo"):
            self.assertIn(key, todo["missing"])

    def test_hourly_rate_is_not_required(self):
        """expert_missing drops it deliberately: booking is free at launch.
        Listing it would send experts hunting for a field that does not gate
        anything."""
        self.assertNotIn("hourly_rate", self._todo()["missing"])

    def test_the_narrow_legacy_flags_are_still_served(self):
        """Additive change — existing consumers must not break."""
        todo = self._todo()
        self.assertIn("needs_payment", todo)
        self.assertIn("needs_location", todo)

    def _todo(self):
        # TeacherDashboardView is [IsAuthenticated] and authenticates through
        # CookieJWTAuthentication, so a session login is not enough.
        client = APIClient()
        client.force_authenticate(user=self.user, token=None)
        res = client.get("/api/skill/teacher/dashboard/")
        self.assertEqual(res.status_code, 200, res.content[:400])
        return res.json()["profile_todo"]
