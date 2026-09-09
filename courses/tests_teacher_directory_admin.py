"""The admin Teachers screen's directory + detail payload.

The screen used to render a teacher as a bare list of subject NAMES. That
identifies nothing an admin can act on: prod carries "Class 10" as the title
of both a CBSE course and an MBSE one (same for Class 8, 9, and every Class
11/12 stream), and 33 of 93 live subject names span more than one course —
"Mathematics" spans twelve. These tests pin the fix: every subject travels
with its course, and that course carries its board, class level, category and
publish status.

Run with:
    DJANGO_SETTINGS_MODULE=config.settings_test .venv/bin/python manage.py test \
        courses.tests_teacher_directory_admin -v2
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Role, TeacherProfile, UserRole
from courses.models import (
    Batch, Board, Course, CourseCategory, Subject, TeachingAssignment,
)

User = get_user_model()

DIRECTORY = "/api/courses/admin/teacher-directory/"


class AdminTeacherDirectoryShapeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="td_admin@x.com", email="td_admin@x.com",
            password="x", is_staff=True,
        )
        cls.role, _ = Role.objects.get_or_create(name="TEACHER")

        cls.cbse = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        cls.mbse = Board.objects.create(name="MBSE", board_type=Board.TYPE_STATE)
        cls.central = CourseCategory.objects.create(
            name="Central Boards", group=CourseCategory.GROUP_BOARDS,
        )

        # The trap this screen exists for: two DIFFERENT courses with the SAME
        # title, told apart only by their board.
        cls.cbse_10 = Course.objects.create(
            title="Class 10", board=cls.cbse, class_level=10,
            status=Course.STATUS_PUBLISHED,
        )
        cls.cbse_10.categories.add(cls.central)
        cls.mbse_10 = Course.objects.create(
            title="Class 10", board=cls.mbse, class_level=10,
            status=Course.STATUS_PUBLISHED,
        )
        # ...and a course nobody can see, which the card must flag.
        cls.draft = Course.objects.create(
            title="NEET", kind=Course.KIND_COACHING, status=Course.STATUS_DRAFT,
        )

        cls.maths_cbse = Subject.objects.create(course=cls.cbse_10, name="Mathematics")
        cls.maths_mbse = Subject.objects.create(course=cls.mbse_10, name="Mathematics")
        cls.bio = Subject.objects.create(course=cls.draft, name="Biology")

        cls.teacher = cls._teacher("td_t1")
        cls.batch = Batch.objects.create(course=cls.cbse_10, name="Batch 2026-27", code="2026-27")

        # The normal prod shape: course-wide AND batch-scoped rows for the same
        # subject (171 of 172 staffed subjects carry both).
        for batch in (None, cls.batch):
            TeachingAssignment.objects.create(
                subject=cls.maths_cbse, batch=batch, teacher=cls.teacher, is_active=True,
            )
        TeachingAssignment.objects.create(
            subject=cls.maths_mbse, teacher=cls.teacher, is_active=True,
        )
        TeachingAssignment.objects.create(
            subject=cls.bio, teacher=cls.teacher, is_active=True,
        )

    @classmethod
    def _teacher(cls, username):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="x",
        )
        UserRole.objects.create(user=user, role=cls.role, is_active=True, is_primary=True)
        TeacherProfile.objects.create(
            user=user, is_approved=True,
            academy_status=TeacherProfile.TRACK_APPROVED,
        )
        return user

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin, token={"context": "teacher"})

    def _row(self, **params):
        r = self.client.get(DIRECTORY, params)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        rows = [x for x in body["data"] if x["user_id"] == str(self.teacher.id)]
        return body, (rows[0] if rows else None)

    def test_same_titled_courses_stay_distinguishable(self):
        """Both "Class 10" courses appear, each carrying its own board."""
        _, row = self._row()
        tens = [g for g in row["courses"] if g["course_title"] == "Class 10"]
        self.assertEqual(len(tens), 2)
        self.assertEqual({g["board"] for g in tens}, {"CBSE", "MBSE"})
        self.assertEqual({g["class_level"] for g in tens}, {10})

    def test_a_subject_carries_its_course_category_and_status(self):
        _, row = self._row()
        cbse = next(g for g in row["courses"] if g["board"] == "CBSE")
        self.assertEqual(cbse["categories"], ["Central Boards"])
        self.assertEqual(cbse["status"], "PUBLISHED")

        draft = next(g for g in row["courses"] if g["course_title"] == "NEET")
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["kind"], "COACHING")
        # A coaching course has no board and no class level — the card must be
        # able to say so rather than render a blank badge.
        self.assertIsNone(draft["board"])
        self.assertIsNone(draft["class_level"])

    def test_course_wide_and_batch_rows_merge_into_one_subject(self):
        """Not two rows saying "Mathematics" — one row saying where it applies."""
        _, row = self._row()
        cbse = next(g for g in row["courses"] if g["board"] == "CBSE")
        self.assertEqual(cbse["subject_count"], 1)
        maths = cbse["subjects"][0]
        self.assertEqual(maths["name"], "Mathematics")
        self.assertTrue(maths["course_wide"])
        self.assertEqual(maths["batches"], ["2026-27"])

    def test_counts_do_not_inflate_on_the_duplicate_rows(self):
        _, row = self._row()
        self.assertEqual(row["course_count"], 3)
        self.assertEqual(row["subject_count"], 3)   # not 4 — the pair is one subject
        self.assertEqual(row["boards"], ["CBSE", "MBSE"])

    def test_coaching_courses_sort_after_school_ones(self):
        _, row = self._row()
        self.assertEqual([g["class_level"] for g in row["courses"]], [10, 10, None])

    def test_filter_options_describe_only_what_is_staffed(self):
        body, _ = self._row()
        self.assertEqual(
            body["filters"]["boards"],
            [{"slug": "cbse", "name": "CBSE"}, {"slug": "mbse", "name": "MBSE"}],
        )
        self.assertEqual(body["filters"]["class_levels"], [10])

    def test_board_filter_separates_the_twins(self):
        other = self._teacher("td_t2")
        # ASSISTANT, not PRIMARY — only one active PRIMARY exists per subject.
        TeachingAssignment.objects.create(
            subject=self.maths_mbse, teacher=other, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT,
        )
        r = self.client.get(DIRECTORY, {"board": "cbse"})
        self.assertEqual(r.status_code, 200, r.content)
        ids = {x["user_id"] for x in r.json()["data"]}
        self.assertIn(str(self.teacher.id), ids)
        self.assertNotIn(str(other.id), ids)

    def test_class_level_filter(self):
        self.assertEqual(len(self._row(class_level="10")[0]["data"]), 1)
        self.assertEqual(self.client.get(DIRECTORY, {"class_level": "11"}).json()["data"], [])

    def test_filter_options_survive_a_facet_being_applied(self):
        """Picking CBSE must not blank out the MBSE chip."""
        body = self.client.get(DIRECTORY, {"board": "cbse"}).json()
        self.assertEqual(
            [b["slug"] for b in body["filters"]["boards"]], ["cbse", "mbse"],
        )

    def test_an_ended_assignment_does_not_appear(self):
        TeachingAssignment.objects.filter(subject=self.bio).update(is_active=False)
        _, row = self._row()
        self.assertNotIn("NEET", [g["course_title"] for g in row["courses"]])
        self.assertEqual(row["course_count"], 2)

    def test_detail_returns_the_same_grouping(self):
        r = self.client.get(f"/api/courses/admin/teachers/{self.teacher.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["course_count"], 3)
        self.assertEqual(
            {g["course_title"] for g in body["courses"]}, {"Class 10", "NEET"},
        )
        # The flat roster is kept for anything reading the old shape, but every
        # row now names its course rather than just its subject.
        self.assertEqual(len(body["assignments"]), 3)
        self.assertTrue(all("course_title" in a for a in body["assignments"]))

    def test_a_teacher_with_nothing_assigned_is_still_listed(self):
        idle = self._teacher("td_idle")
        r = self.client.get(DIRECTORY)
        row = next(x for x in r.json()["data"] if x["user_id"] == str(idle.id))
        self.assertEqual(row["courses"], [])
        self.assertEqual(row["course_count"], 0)
        self.assertEqual(row["subject_count"], 0)
        self.assertEqual(row["boards"], [])

    def test_board_and_class_apply_to_the_same_course(self):
        """Two chained filters on a multi-valued relation would match a teacher
        who covers something CBSE and, separately, something in class 11 —
        which is not what "CBSE Class 11" asks for."""
        cbse_11 = Course.objects.create(
            title="Class 11 Science", board=self.cbse, class_level=11,
            status=Course.STATUS_PUBLISHED,
        )
        physics = Subject.objects.create(course=cbse_11, name="Physics")
        mbse_11 = Course.objects.create(
            title="Class 11 Science", board=self.mbse, class_level=11,
            status=Course.STATUS_PUBLISHED,
        )
        # This teacher holds CBSE (class 10) and class 11 (MBSE) — but never
        # CBSE class 11.
        split = self._teacher("td_split")
        TeachingAssignment.objects.create(
            subject=self.maths_cbse, teacher=split, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT,
        )
        TeachingAssignment.objects.create(
            subject=Subject.objects.create(course=mbse_11, name="Physics"),
            teacher=split, is_active=True,
        )
        TeachingAssignment.objects.create(
            subject=physics, teacher=self.teacher, is_active=True,
        )

        r = self.client.get(DIRECTORY, {"board": "cbse", "class_level": "11"})
        ids = {x["user_id"] for x in r.json()["data"]}
        self.assertEqual(ids, {str(self.teacher.id)})
