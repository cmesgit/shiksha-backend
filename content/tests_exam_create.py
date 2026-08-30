"""Tests for the competitive-exam create endpoints.

What matters here is the invisibility traps, not the happy path. An exam can
be created successfully and still be unreachable by every visitor, in three
independent ways, and all three are silent:

  * no competitive category linked  -> not in the navbar, not in the catalog's
    competitive axis (both key on the category, NOT on kind="COACHING")
  * status left at DRAFT            -> below PUBLIC_COURSE_STATUSES entirely
  * a second category with the same -> CourseCategory.save() appends "-2"
    slug                               instead of refusing, so the new one
                                       lists nothing

So these tests assert on what the public surfaces would actually resolve.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from content.models import ShowcaseCourse
from courses.models import Course, CourseCategory, CourseDetail
from global_settings.models import GlobalSettings

User = get_user_model()


class ExamCreateTestBase(TestCase):
    def setUp(self):
        # IsStudioEditor is a real gate: staff AND the flag. It caches for 60s,
        # so clear the cache rather than relying on test ordering.
        from django.core.cache import cache

        cache.clear()
        settings_row = GlobalSettings.load()
        settings_row.content_studio_enabled = True
        settings_row.save()

        self.staff = User.objects.create_user(
            email="studio@shiksha.test", username="studio",
            password="x", is_staff=True,
        )
        # DRF's APIClient, not Django's test client: the only authentication
        # class configured is accounts.authentication.CookieJWTAuthentication,
        # so a session login authenticates nothing and every request 401s.
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)

        self.category = CourseCategory.objects.create(
            name="NEET", group=CourseCategory.GROUP_COMPETITIVE,
        )

    def create_url(self):
        return reverse("content:studio-exam-create")

    def options_url(self):
        return reverse("content:studio-exam-options")

    def post_exam(self, **overrides):
        payload = {
            "name": "NEET Preparation",
            "description": "Full-length NEET coaching.",
            "category": {"id": self.category.id},
            "status": "COMING_SOON",
        }
        payload.update(overrides)
        return self.client.post(self.create_url(), payload, format="json")


class ExamOptionsTest(ExamCreateTestBase):
    def test_lists_only_competitive_categories(self):
        CourseCategory.objects.create(
            name="Class 10", group=CourseCategory.GROUP_SCHOOL,
        )
        response = self.client.get(self.options_url())
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.json()["categories"]]
        self.assertIn("NEET", names)
        self.assertNotIn("Class 10", names)

    def test_flags_categories_that_have_no_course(self):
        response = self.client.get(self.options_url())
        neet = next(c for c in response.json()["categories"] if c["name"] == "NEET")
        self.assertFalse(neet["has_course"])

        self.post_exam()

        response = self.client.get(self.options_url())
        neet = next(c for c in response.json()["categories"] if c["name"] == "NEET")
        self.assertTrue(neet["has_course"])

    def test_every_status_states_its_consequence(self):
        response = self.client.get(self.options_url())
        statuses = response.json()["statuses"]
        self.assertTrue(statuses)
        for entry in statuses:
            self.assertTrue(entry["consequence"].strip())

    def test_requires_staff(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(self.options_url()).status_code, (401, 403))


class ExamCreateTest(ExamCreateTestBase):
    def test_creates_the_whole_exam_in_one_call(self):
        response = self.post_exam(
            detail={
                "level": "Advanced", "duration_weeks": 52,
                "language": "English", "syllabus": "Physics, Chemistry, Biology",
                "highlights": "Daily practice", "includes": "Mock tests",
            },
            card={"create": True, "fact_line": "720 marks", "icon": "pulse"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()

        course = Course.objects.get(pk=body["course"]["id"])
        self.assertEqual(course.kind, "COACHING")
        self.assertEqual(course.status, "COMING_SOON")
        self.assertIsNone(course.board)
        self.assertEqual(list(course.categories.all()), [self.category])

        self.assertTrue(CourseDetail.objects.filter(course=course).exists())
        self.assertTrue(ShowcaseCourse.objects.filter(course=course).exists())
        self.assertTrue(body["in_navbar"])

    def test_the_new_exam_actually_reaches_the_public_navbar(self):
        """The assertion that matters. Everything else is bookkeeping."""
        from courses.models import Course as C

        self.post_exam(status="PUBLISHED")

        # Same query PublicNavMenuView runs.
        listed = (
            C.objects
            .filter(
                status__in=("PUBLISHED", "COMING_SOON"),
                categories__group=CourseCategory.GROUP_COMPETITIVE,
            )
            .distinct()
        )
        self.assertIn("NEET Preparation", [c.title for c in listed])

    def test_slug_is_generated_and_returned(self):
        """The navbar emits /courses/<slug>, and slug is never re-derived on
        rename — so it has to be right the first time and visible to whoever
        created it."""
        response = self.post_exam()
        body = response.json()
        self.assertTrue(body["course"]["slug"])
        self.assertEqual(
            body["course"]["public_url"], f"/courses/{body['course']['slug']}"
        )

    def test_creating_a_new_category_inline(self):
        response = self.post_exam(
            name="CLAT Preparation", category={"name": "CLAT"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["category"]["created"])
        category = CourseCategory.objects.get(name="CLAT")
        self.assertEqual(category.group, CourseCategory.GROUP_COMPETITIVE)

    def test_refuses_a_duplicate_category_slug(self):
        """CourseCategory.save() appends '-2' rather than refusing, so without
        this check a near-duplicate name silently makes a SECOND category that
        lists nothing."""
        response = self.post_exam(
            name="Another Exam", category={"name": "  neet "},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.json())
        self.assertEqual(CourseCategory.objects.filter(slug="neet").count(), 1)

    def test_refuses_a_duplicate_course_title(self):
        self.post_exam()
        response = self.post_exam()
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json())
        self.assertEqual(Course.objects.filter(title="NEET Preparation").count(), 1)

    def test_refuses_a_non_competitive_category(self):
        school = CourseCategory.objects.create(
            name="Class 12", group=CourseCategory.GROUP_SCHOOL,
        )
        response = self.post_exam(category={"id": school.id})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Course.objects.filter(title="NEET Preparation").exists())

    def test_requires_a_category(self):
        response = self.post_exam(category={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.json())

    def test_requires_a_name(self):
        response = self.post_exam(name="   ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json())

    def test_draft_says_so_in_next_steps(self):
        """The existing wizard created DRAFT exams and said nothing, so they
        looked created and were invisible."""
        response = self.post_exam(status="DRAFT")
        body = response.json()
        self.assertFalse(body["in_navbar"])
        self.assertTrue(
            any("draft" in step.lower() for step in body["next_steps"])
        )

    def test_card_is_linked_to_the_competitive_axis(self):
        """A board-less competitive course needs link_state pointing at the
        competitive axis, or the card drops the visitor on an unfiltered
        catalog that cannot show it."""
        response = self.post_exam(card={"create": True})
        card = ShowcaseCourse.objects.get(pk=response.json()["card_id"])
        self.assertEqual(card.link_state, {"selectedBoardGroup": "competitive"})
        self.assertEqual(card.link_path, "/courses")

    def test_no_card_when_not_asked_for(self):
        response = self.post_exam()
        self.assertIsNone(response.json()["card_id"])
        self.assertFalse(ShowcaseCourse.objects.exists())
        self.assertTrue(
            any("card" in step.lower() for step in response.json()["next_steps"])
        )

    def test_a_bad_card_rolls_the_whole_exam_back(self):
        """Everything or nothing — a course whose card silently failed is the
        half-built state this endpoint exists to prevent."""
        response = self.post_exam(
            card={"create": True, "icon": "not-a-real-icon-key"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Course.objects.filter(title="NEET Preparation").exists())
        self.assertFalse(CourseDetail.objects.exists())
        self.assertFalse(ShowcaseCourse.objects.exists())

    def test_requires_staff(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.create_url(), {}, format="json")
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Course.objects.filter(kind="COACHING").exists())

    def test_refused_when_the_studio_flag_is_off(self):
        from django.core.cache import cache

        settings_row = GlobalSettings.load()
        settings_row.content_studio_enabled = False
        settings_row.save()
        cache.clear()

        response = self.post_exam()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Course.objects.filter(title="NEET Preparation").exists())


class ExamCreateAppearsInReadinessTest(ExamCreateTestBase):
    def test_a_created_exam_shows_up_on_the_readiness_screen(self):
        """The two endpoints have to agree — the create form's whole purpose is
        to fill the readiness screen's empty state."""
        self.post_exam()
        response = self.client.get(reverse("content:studio-exam-readiness"))
        self.assertEqual(response.status_code, 200)
        names = [e["name"] for e in response.json()["exams"]]
        self.assertIn("NEET Preparation", names)
