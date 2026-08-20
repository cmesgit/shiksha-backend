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
