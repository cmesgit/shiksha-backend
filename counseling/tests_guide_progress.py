"""Reading progress is persistent, idempotent, and scoped to a learner."""

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounts.models import LearnerProfile
from counseling.guide_models import CareerGuide, GuideChapter


class GuideProgressTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.reader = User.objects.create_user(
            email="guide-reader@example.com", username="guidereader", password="password123")
        LearnerProfile.objects.create(
            account=cls.reader, display_name="Guide Reader", is_default=True)
        cls.other = User.objects.create_user(
            email="guide-other@example.com", username="guideother", password="password123")
        LearnerProfile.objects.create(
            account=cls.other, display_name="Other Reader", is_default=True)
        cls.guide = CareerGuide.objects.create(
            slug="career-guide-test", title="Career Guide", audience="Students",
            stage="secondary", stage_label="Secondary", status="published")
        cls.chapter = GuideChapter.objects.create(
            guide=cls.guide, slug="chapter-one", number=1, title="First chapter")
        cls.other_guide = CareerGuide.objects.create(
            slug="other-guide-test", title="Other Guide", audience="Students",
            stage="secondary", stage_label="Secondary", status="published")
        GuideChapter.objects.create(
            guide=cls.other_guide, slug="foreign-chapter", number=1, title="Foreign chapter")

    def url(self):
        return "/api/counseling/guide-progress/career-guide-test/"

    def test_progress_survives_reads_and_duplicate_marks(self):
        self.client.force_authenticate(self.reader)
        initial = self.client.get(self.url())
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.data["read_count"], 0)
        self.assertIsNone(initial.data["last_chapter_slug"])

        opened = self.client.put(self.url(), {"chapter_slug": "chapter-one"}, format="json")
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertEqual(opened.data["last_chapter_slug"], "chapter-one")
        self.assertEqual(opened.data["read_count"], 0)

        for _ in range(2):
            marked = self.client.put(self.url(),
                                     {"chapter_slug": "chapter-one", "mark_read": True}, format="json")
        self.assertEqual(marked.data["read_count"], 1)
        self.assertTrue(marked.data["has_read"])
        listing = self.client.get("/api/counseling/guide-progress/")
        self.assertEqual(len(listing.data["results"]), 1)
        self.assertEqual(listing.data["results"][0]["last_chapter_title"], "First chapter")

    def test_other_user_cannot_see_progress(self):
        self.client.force_authenticate(self.reader)
        self.client.put(self.url(), {"chapter_slug": "chapter-one", "mark_read": True}, format="json")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/counseling/guide-progress/").data["results"], [])
        self.assertEqual(self.client.get(self.url()).data["read_count"], 0)

    def test_guide_counts_as_read_only_after_all_chapters(self):
        GuideChapter.objects.create(
            guide=self.guide, slug="chapter-two", number=2, title="Second chapter")
        self.client.force_authenticate(self.reader)
        first = self.client.put(self.url(),
            {"chapter_slug": "chapter-one", "mark_read": True}, format="json")
        self.assertEqual(first.data["read_count"], 1)
        self.assertFalse(first.data["has_read"])
        second = self.client.put(self.url(),
            {"chapter_slug": "chapter-two", "mark_read": True}, format="json")
        self.assertEqual(second.data["read_count"], 2)
        self.assertTrue(second.data["has_read"])

    def test_rejects_chapter_from_another_guide_and_non_boolean_mark(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.put(self.url(),
            {"chapter_slug": "foreign-chapter", "mark_read": True}, format="json").status_code, 400)
        self.assertEqual(self.client.put(self.url(),
            {"chapter_slug": "chapter-one", "mark_read": "yes"}, format="json").status_code, 400)

    def test_anonymous_cannot_access_progress(self):
        self.assertIn(self.client.get(self.url()).status_code, (401, 403))
        self.assertIn(self.client.get("/api/counseling/guide-progress/").status_code, (401, 403))
