from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, User

# Create your tests here.


class MeViewFeatureFlagsTest(TestCase):
    """design_handoff_quiz_system Phase 0 — GET /api/accounts/me/ exposes a
    read-only `feature_flags` dict sourced from GlobalSettings, so the teacher
    app (and later the student app) can gate quiz-v2 UI without a second
    request. Phase 0 ships both flags OFF and consumes neither — this pins the
    shape so Phase 5+ can rely on it.
    """

    URL = "/api/accounts/me/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="priya", email="priya@example.com", password="whatever-9",
        )
        LearnerProfile.objects.create(
            account=self.user, display_name="Priya", relationship="SELF", is_default=True,
        )
        self.client_ = APIClient()
        self.client_.force_authenticate(user=self.user)

    def test_feature_flags_present_with_both_keys_false_by_default(self):
        res = self.client_.get(self.URL)
        self.assertEqual(res.status_code, 200, res.content)
        flags = res.json()["feature_flags"]
        self.assertIn("quiz_v2_enabled", flags)
        self.assertIn("ai_question_drafting_enabled", flags)
        self.assertFalse(flags["quiz_v2_enabled"])
        self.assertFalse(flags["ai_question_drafting_enabled"])

    def test_feature_flags_reflect_globalsettings_when_flipped(self):
        from global_settings.models import GlobalSettings

        gs = GlobalSettings.load()
        gs.quiz_v2_enabled = True
        gs.ai_question_drafting_enabled = True
        gs.save(update_fields=["quiz_v2_enabled", "ai_question_drafting_enabled"])

        res = self.client_.get(self.URL)
        flags = res.json()["feature_flags"]
        self.assertTrue(flags["quiz_v2_enabled"])
        self.assertTrue(flags["ai_question_drafting_enabled"])
