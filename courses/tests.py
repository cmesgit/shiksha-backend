"""Teacher roster endpoints: one row per STUDENT, not per account.

Regression cover for the multi-profile bug in ``TeacherAllStudentsView`` /
``SubjectStudentsView``: both used to emit ``id = User.id`` and the all-students
view deduped on it, so a parent with three enrolled children collapsed into a
single row identified by the family account rather than the student.
"""

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import LearnerProfile, Role, User, UserRole
from enrollments.models import Enrollment

from .models import Course, Subject, SubjectTeacher

ALL_STUDENTS_URL = "/api/courses/teacher/all-students/"


class TeacherRosterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)

        cls.teacher = User.objects.create_user(
            username="teach", email="teach@example.com", password="pw",
        )
        UserRole.objects.create(user=cls.teacher, role=teacher_role)

        cls.course = Course.objects.create(title="Class 10 Science")
        cls.other_course = Course.objects.create(title="Class 10 Maths")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.other_subject = Subject.objects.create(
            course=cls.other_course, name="Algebra",
        )
        # The teacher teaches one subject in each course, so both courses are
        # in scope for the all-students view.
        SubjectTeacher.objects.create(subject=cls.subject, teacher=cls.teacher)
        SubjectTeacher.objects.create(subject=cls.other_subject, teacher=cls.teacher)

        # ── One account, three enrolled children: 1 user, 3 students. ──
        cls.parent = User.objects.create_user(
            username="parent", email="parent@example.com", password="pw",
        )
        cls.children = [
            LearnerProfile.objects.create(
                account=cls.parent,
                display_name=name,
                full_name=name,
                relationship="SON",
                is_default=(name == "Aaron Doe"),
            )
            for name in ("Aaron Doe", "Bina Doe", "Chandan Doe")
        ]
        for child in cls.children:
            Enrollment.objects.create(
                user=cls.parent,
                learner_profile=child,
                course=cls.course,
                status=Enrollment.STATUS_ACTIVE,
            )

        # ── A legacy enrollment: learner_profile is NULL, so it must be
        # attributed to the account's DEFAULT profile (the _dual_key_q rule). ──
        cls.legacy_account = User.objects.create_user(
            username="legacy", email="legacy@example.com", password="pw",
        )
        cls.legacy_default = LearnerProfile.objects.create(
            account=cls.legacy_account,
            display_name="Legacy Learner",
            full_name="Legacy Learner",
            is_default=True,
        )
        # A non-default sibling that must NOT absorb the legacy row.
        cls.legacy_sibling = LearnerProfile.objects.create(
            account=cls.legacy_account,
            display_name="Younger Sibling",
            full_name="Younger Sibling",
            relationship="SON",
        )
        Enrollment.objects.create(
            user=cls.legacy_account,
            learner_profile=None,
            course=cls.course,
            status=Enrollment.STATUS_ACTIVE,
        )

    def setUp(self):
        # DRF is configured with CookieJWTAuthentication only (no session auth),
        # so force_login() would leave the request anonymous.
        access = RefreshToken.for_user(self.teacher).access_token
        self.client.cookies["access"] = str(access)

    # ───────────────────────────── all students ─────────────────────────────
    def test_siblings_are_separate_rows_keyed_on_the_learner_profile(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        family = [r for r in rows if r["email"] == "parent@example.com"]
        self.assertEqual(len(family), 3, "each enrolled child must get its own row")
        self.assertEqual(
            {r["id"] for r in family},
            {str(c.id) for c in self.children},
            "row id must be the learner-profile id, not the account id",
        )
        # The account is still reported, just not as the student's identity.
        self.assertEqual({r["account_id"] for r in family}, {str(self.parent.id)})
        self.assertNotIn(str(self.parent.id), {r["id"] for r in rows})

    def test_legacy_null_profile_row_resolves_to_the_default_profile(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        legacy = [r for r in rows if r["email"] == "legacy@example.com"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["id"], str(self.legacy_default.id))
        self.assertFalse(legacy[0]["unresolved_profile"])
        self.assertNotIn(
            str(self.legacy_sibling.id),
            {r["id"] for r in rows},
            "a non-default sibling must not be credited with a legacy enrollment",
        )

    def test_total_students_counts_students_not_accounts(self):
        body = self.client.get(ALL_STUDENTS_URL).json()
        # 3 children + 1 legacy student, from only 2 accounts.
        self.assertEqual(body["total_students"], 4)
        self.assertEqual(len(body["students"]), 4)

    def test_one_student_in_two_of_the_teachers_courses_is_one_row(self):
        Enrollment.objects.create(
            user=self.parent,
            learner_profile=self.children[0],
            course=self.other_course,
            status=Enrollment.STATUS_ACTIVE,
        )
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        matching = [r for r in rows if r["id"] == str(self.children[0].id)]
        self.assertEqual(len(matching), 1, "dedupe is per student, per roster")
        self.assertEqual(
            sorted(matching[0]["course_titles"]),
            ["Class 10 Maths", "Class 10 Science"],
            "every shared course is listed, not just whichever came first",
        )

    def test_rows_are_sorted_by_name_including_legacy_rows(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]
        names = [r["full_name"] for r in rows]
        self.assertEqual(names, sorted(names, key=str.lower))
        # Legacy rows sort by their resolved profile's name, not by NULL.
        self.assertIn("Legacy Learner", names)

    # ─────────────────────────── subject students ───────────────────────────
    def test_subject_students_are_keyed_on_the_learner_profile(self):
        url = f"/api/courses/subjects/{self.subject.id}/students/"
        body = self.client.get(url).json()

        self.assertEqual(body["total_students"], 4)
        ids = {r["id"] for r in body["students"]}
        self.assertEqual(
            ids,
            {str(c.id) for c in self.children} | {str(self.legacy_default.id)},
        )
        self.assertNotIn(str(self.parent.id), ids)

    def test_subject_students_dedupes_a_legacy_and_migrated_pair(self):
        """unique_together("learner_profile", "course") doesn't bind NULL, so an
        account can carry both a legacy and a migrated row for one course."""
        Enrollment.objects.create(
            user=self.legacy_account,
            learner_profile=self.legacy_default,
            course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )
        url = f"/api/courses/subjects/{self.subject.id}/students/"
        rows = self.client.get(url).json()["students"]

        self.assertEqual(
            [r["id"] for r in rows].count(str(self.legacy_default.id)),
            1,
            "the same student must not appear twice",
        )
