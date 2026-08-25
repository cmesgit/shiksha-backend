from django.test import TestCase
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from courses.models import Board

User = get_user_model()


class CompetitiveBoardTypeTests(TestCase):
    """A competitive exam (MPSC, UPSC, NEET) is a syllabus authority a course
    hangs off, exactly like a board — but it is neither central nor state, and
    the catalog previously had nowhere to put one."""

    def test_a_competitive_exam_can_be_created(self):
        b = Board.objects.create(name="UPSC", board_type=Board.TYPE_COMPETITIVE)
        self.assertEqual(b.board_type, "COMPETITIVE")

    def test_admin_can_create_one_through_the_api(self):
        admin = User.objects.create_user(username="cb@x.com", email="cb@x.com",
                                         password="x", is_staff=True)
        c = APIClient()
        c.force_authenticate(user=admin, token={"context": "teacher"})
        r = c.post("/api/courses/admin/boards/",
                   {"name": "NEET", "board_type": "COMPETITIVE"}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertTrue(Board.objects.filter(
            name="NEET", board_type="COMPETITIVE").exists())

    def test_the_public_catalog_exposes_the_type(self):
        Board.objects.create(name="JEE", board_type=Board.TYPE_COMPETITIVE)
        r = APIClient().get("/api/courses/public/boards/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json() if isinstance(r.json(), list) else r.json().get("results", [])
        jee = [b for b in rows if b.get("name") == "JEE"]
        if jee:
            self.assertEqual(jee[0]["board_type"], "COMPETITIVE")


class CompetitiveCourseAdminWorkflowTests(TestCase):
    """The Admin dashboard's course form now sends `kind` and `class_level`.

    Both were writable on CourseSerializer all along, but the form's payload
    omitted them — so every course created through the UI was silently
    ACADEMIC with no class level, and a COACHING (competitive) course could
    only be made via the create_competitive_courses management command or
    Django admin. These tests pin the round-trip the form depends on.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cc@x.com", email="cc@x.com", password="x", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin, token={"context": "teacher"})

    def test_admin_can_create_a_coaching_course_with_no_class_level(self):
        r = self.client.post("/api/courses/admin/courses/", {
            "title": "UPSC & Civil Services",
            "description": "Prelims + Mains",
            "price": 0,
            "kind": "COACHING",
            "class_level": None,
            "status": "COMING_SOON",
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        from courses.models import Course
        c = Course.objects.get(title="UPSC & Civil Services")
        self.assertEqual(c.kind, Course.KIND_COACHING)
        self.assertIsNone(c.class_level, "a coaching course spans no single class")

    def test_admin_can_create_an_academic_course_with_a_class_level(self):
        r = self.client.post("/api/courses/admin/courses/", {
            "title": "Class 9 Foundation",
            "price": 0,
            "kind": "ACADEMIC",
            "class_level": 9,
            "status": "PUBLISHED",
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        from courses.models import Course
        c = Course.objects.get(title="Class 9 Foundation")
        self.assertEqual(c.kind, Course.KIND_ACADEMIC)
        self.assertEqual(c.class_level, 9)

    def test_kind_and_class_level_are_readable_back_for_the_edit_form(self):
        from courses.models import Course
        c = Course.objects.create(title="NEET Preparation", price=0,
                                  kind=Course.KIND_COACHING, class_level=None)
        r = self.client.get(f"/api/courses/admin/courses/{c.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        # Without these the edit form silently resets a coaching course to
        # ACADEMIC the first time anyone opens and saves it.
        self.assertEqual(r.json()["kind"], "COACHING")
        self.assertIsNone(r.json()["class_level"])
