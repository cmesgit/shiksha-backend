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


class CompetitiveCoursesAreReachableFromThePublicCatalogTests(TestCase):
    """A competitive course must be findable at /courses, not just in the nav.

    The catalog is board-scoped: competitive courses are created with
    board=NULL (see create_competitive_courses), so every board-filtered query
    excludes them by construction. The public catalog endpoint already
    accepted ?group= and ?kind=, but nothing sent them and the response
    omitted both fields, so a client could not tell a competitive row apart
    from an academic one even if it got one back.

    These pin the contract the rebuilt catalog depends on.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Course, CourseCategory

        cls.competitive_cat = CourseCategory.objects.create(
            name="UPSC", slug="upsc", group=CourseCategory.GROUP_COMPETITIVE,
        )
        cls.board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)

        cls.competitive = Course.objects.create(
            title="UPSC Civil Services", price=0,
            kind=Course.KIND_COACHING,
            status=Course.STATUS_COMING_SOON,
            board=None, class_level=None,
        )
        cls.competitive.categories.add(cls.competitive_cat)

        cls.academic = Course.objects.create(
            title="Class 10 Science", price=0,
            kind=Course.KIND_ACADEMIC,
            status=Course.STATUS_PUBLISHED,
            board=cls.board, class_level=10,
        )

    def _catalog(self, **params):
        r = APIClient().get("/api/courses/public/catalog/", params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_a_boardless_query_returns_the_competitive_course(self):
        """The frontend used to hard-return [] without a board id, so this
        shape was never exercised. It is the ONLY way a board=NULL course can
        reach the catalog page."""
        titles = [c["title"] for c in self._catalog()]
        self.assertIn("UPSC Civil Services", titles)
        self.assertIn("Class 10 Science", titles)

    def test_filtering_by_group_returns_only_competitive(self):
        rows = self._catalog(group="competitive")
        self.assertEqual([c["title"] for c in rows], ["UPSC Civil Services"])

    def test_filtering_by_board_still_excludes_competitive(self):
        """Not a bug to fix — this is why competitive needs its own axis
        rather than a board chip."""
        titles = [c["title"] for c in self._catalog(board=str(self.board.id))]
        self.assertEqual(titles, ["Class 10 Science"])

    def test_the_row_carries_kind_and_category_groups(self):
        """Both, deliberately: they can disagree. A COACHING course with no
        category link is invisible to the nav, and this is the only place a
        client can see that."""
        row = next(c for c in self._catalog() if c["title"] == "UPSC Civil Services")
        self.assertEqual(row["kind"], "COACHING")
        self.assertEqual(row["category_groups"], ["competitive"])
        self.assertIsNone(row["board"])
        self.assertIsNone(row["class_level"])
        self.assertTrue(row["is_coming_soon"])

        academic = next(c for c in self._catalog() if c["title"] == "Class 10 Science")
        self.assertEqual(academic["kind"], "ACADEMIC")
        self.assertEqual(academic["category_groups"], [])


class ShowcaseCardLinkStateTests(TestCase):
    """A homepage showcase card must deep-link somewhere that can show it.

    Two bugs, both silent:
      * a competitive course got link_state = {}, dumping the visitor on the
        catalog with no filter — and until the catalog gained a competitive
        axis, that catalog could not display the course at all;
      * a board-linked card sent the board's LOWERCASED NAME where the catalog
        resolves on SLUG, so any multi-word board ("BSE Odisha" -> "bse
        odisha" vs slug "bseodisha") silently fell through to the default.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Course, CourseCategory
        from content.models import ShowcaseCourse

        cls.ShowcaseCourse = ShowcaseCourse
        cls.multiword_board = Board.objects.create(
            name="BSE Odisha", slug="bseodisha", board_type=Board.TYPE_STATE,
        )
        cls.academic = Course.objects.create(
            title="Class 10", price=0, board=cls.multiword_board, class_level=10,
        )
        cat = CourseCategory.objects.create(
            name="NEET", slug="neet", group=CourseCategory.GROUP_COMPETITIVE,
        )
        cls.competitive = Course.objects.create(
            title="NEET Preparation", price=0, kind=Course.KIND_COACHING, board=None,
        )
        cls.competitive.categories.add(cat)
        # No category link at all — the state create_competitive_courses
        # produces when the categories were never seeded.
        cls.orphan_coaching = Course.objects.create(
            title="Orphan Coaching", price=0, kind=Course.KIND_COACHING, board=None,
        )

    def _link_state(self, course):
        from content.admin_serializers import ShowcaseCourseAdminSerializer
        s = ShowcaseCourseAdminSerializer(data={
            "title": course.title, "level_label": "Class 10",
            "categories": ["competitive"], "course": str(course.id),
        })
        self.assertTrue(s.is_valid(), s.errors)
        return s.validated_data["link_state"]

    def test_board_linked_card_sends_the_slug_not_the_name(self):
        state = self._link_state(self.academic)
        self.assertEqual(state["selectedBoard"], "bseodisha")
        self.assertNotEqual(state["selectedBoard"], "bse odisha")
        self.assertEqual(state["selectedBoardGroup"], "state")

    def test_competitive_card_deep_links_to_the_competitive_axis(self):
        self.assertEqual(
            self._link_state(self.competitive),
            {"selectedBoardGroup": "competitive"},
        )

    def test_a_coaching_course_with_no_category_still_resolves(self):
        """Keying on the category group alone would miss exactly the courses
        most likely to be misfiled."""
        self.assertEqual(
            self._link_state(self.orphan_coaching),
            {"selectedBoardGroup": "competitive"},
        )
