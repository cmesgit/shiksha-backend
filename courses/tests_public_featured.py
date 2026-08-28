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


class LinkedCardOverrideTests(TestCase):
    """A linked card derives its title, price and picture from the course. That
    keeps the homepage honest when a course is renamed, but left no way for a
    card to say anything the course didn't.

    Precedence is deliberately NOT flipped to "the card's value wins if set":
    every linked card on prod already carries a stale `title` from before it
    was linked (one holds "CBSE (Central Board)" while rendering the Board's
    "CBSE") and nine carry a stale price_label. Flipping would have silently
    rewritten the live homepage, so the escape hatch is opt-in per card.
    """

    def setUp(self):
        self.client = APIClient()
        cache.clear()

    def _card(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        return res.data["cards"][0]

    def test_by_default_the_linked_course_still_wins(self):
        course = Course.objects.create(title="Class 10 Science", price=150000)
        ShowcaseCourse.objects.create(
            title="Stale title from before linking", price_label="1,500",
            course=course, order=0,
        )
        card = self._card()
        self.assertEqual(card["title"], "Class 10 Science")
        self.assertEqual(card["price_label"], "₹1,500/month")

    def test_opting_out_lets_the_card_speak_for_itself(self):
        course = Course.objects.create(title="Class 10 Science", price=150000)
        ShowcaseCourse.objects.create(
            title="Our flagship science programme", price_label="Talk to us",
            course=course, order=0, use_own_details=True,
        )
        card = self._card()
        self.assertEqual(card["title"], "Our flagship science programme")
        self.assertEqual(card["price_label"], "Talk to us")

    def test_opting_out_does_not_unlink_the_card(self):
        """The link still drives where the card sends people — opting out is
        about the words on it, not the destination."""
        course = Course.objects.create(title="Class 10 Science", slug="class-10-science")
        ShowcaseCourse.objects.create(
            title="Own words", course=course, order=0, use_own_details=True,
        )
        card = self._card()
        self.assertEqual(card["course_slug"], "class-10-science")

    def test_coming_soon_follows_the_course_by_default(self):
        course = Course.objects.create(
            title="NEET Preparation", status=Course.STATUS_COMING_SOON,
        )
        ShowcaseCourse.objects.create(title="x", course=course, order=0)
        self.assertTrue(self._card()["is_coming_soon"])

    def test_the_badge_can_be_forced_on_for_a_published_course(self):
        course = Course.objects.create(title="Live course")
        ShowcaseCourse.objects.create(
            title="x", course=course, order=0, coming_soon_override=True,
        )
        self.assertTrue(self._card()["is_coming_soon"])

    def test_the_badge_can_be_forced_off_for_a_coming_soon_course(self):
        course = Course.objects.create(
            title="NEET Preparation", status=Course.STATUS_COMING_SOON,
        )
        ShowcaseCourse.objects.create(
            title="x", course=course, order=0, coming_soon_override=False,
        )
        self.assertFalse(self._card()["is_coming_soon"])

    def test_the_two_switches_are_independent(self):
        """A card can show its own title and still follow the course's launch
        state — they are separate decisions."""
        course = Course.objects.create(
            title="Course name", status=Course.STATUS_COMING_SOON,
        )
        ShowcaseCourse.objects.create(
            title="My own name", course=course, order=0, use_own_details=True,
        )
        card = self._card()
        self.assertEqual(card["title"], "My own name")
        self.assertTrue(card["is_coming_soon"], "override left unset must still follow the course")
