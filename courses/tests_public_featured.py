"""GET /courses/public/featured/ — the homepage 'Featured courses' grid.

Regression cover for the card->course link. The route the homepage sends
visitors to, /courses/:slug, resolves by SLUG (Courses.jsx ->
getPublicCourseBySlug -> get_object_or_404(slug=...)), but this payload used to
carry only `course_id`. The frontend built `/courses/<uuid>` from it, which
404'd and silently dropped the visitor on the bare catalog — so every
"Enroll now" on a course-linked homepage card was dead, and there was no way to
link a "View syllabus" control either.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import ShowcaseCourse

from .models import Course

URL = "/api/courses/public/featured/"


class PublicFeaturedSlugTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # This view caches on the request, and these tests assert on freshly
        # added rows, so a leftover entry would mask everything.
        cache.clear()

    def _cards(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        return res.data["cards"]

    def test_linked_card_exposes_the_courses_slug(self):
        course = Course.objects.create(title="Class 10 Science")
        ShowcaseCourse.objects.create(
            title="ignored — the linked course wins", course=course, order=0,
        )
        card = self._cards()[0]
        self.assertEqual(card["course_slug"], course.slug)
        self.assertEqual(card["course_id"], str(course.id))
        self.assertTrue(card["course_slug"], "slug must be non-empty to be linkable")

    def test_unlinked_card_reports_no_slug_rather_than_omitting_it(self):
        # A showcase row with no course is a category tile ("Explore
        # Programs"), and the frontend keys "should I offer a syllabus link"
        # off this being falsy — so it has to be present and null, not missing.
        ShowcaseCourse.objects.create(title="Competitive Exams", order=0)
        card = self._cards()[0]
        self.assertIn("course_slug", card)
        self.assertIsNone(card["course_slug"])

    def test_slug_survives_a_course_title_change(self):
        # Slugs are what the URL is built from, so if a retitle reslugs the
        # course the card must follow it rather than serving a stale path.
        course = Course.objects.create(title="Class 9 Maths")
        ShowcaseCourse.objects.create(title="Class 9 Maths", course=course, order=0)
        first = self._cards()[0]["course_slug"]

        course.title = "Class 9 Mathematics"
        course.save()
        cache.clear()

        course.refresh_from_db()
        self.assertEqual(self._cards()[0]["course_slug"], course.slug)
        self.assertTrue(first)
