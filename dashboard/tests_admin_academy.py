"""Cover for the admin console's academy-content list and option tree.

The traps here are the ones that produce a form which looks right and cannot
succeed, or a list that looks empty and is not:

  * the option tree offering a teacher for a batch they do not cover, so the
    assignment endpoint 400s on a choice the console itself presented;
  * a nameless TeachingAssignment (teacher SET_NULL, and prod has active
    PRIMARY rows with nobody in them) rendering as a blank picker entry;
  * a teacher holding BOTH a course-wide and a batch-scoped row on one subject
    being reduced to whichever row was read last;
  * the admin list inheriting the teacher scope and therefore showing an admin
    only the subjects they teach — which is none of them;
  * `teacher_id=junk` reaching a UUID column and 500ing the screen.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole
from assignments.models import Assignment
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from materials.models import StudyMaterial

OPTIONS = "/api/dashboard/admin/academy/options/"
RESOURCES = "/api/dashboard/admin/academy/resources/"


class AdminAcademyBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")

        cls.admin = User.objects.create_user(
            username="boss", email="boss@test.com", password="x",
            is_staff=True, first_name="Ada", last_name="Boss")
        cls.teacher = User.objects.create_user(
            username="t1", email="t1@test.com", password="x",
            first_name="Tara", last_name="One")
        cls.batch_only = User.objects.create_user(
            username="t2", email="t2@test.com", password="x",
            first_name="Bala", last_name="Two")
        cls.unstaffed_teacher = User.objects.create_user(
            username="t3", email="t3@test.com", password="x",
            first_name="Cyrus", last_name="Three")
        for u in (cls.teacher, cls.batch_only, cls.unstaffed_teacher):
            UserRole.objects.create(
                user=u, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(
            course=cls.course, name="Mechanics", order=1)
        cls.bare_subject = Subject.objects.create(
            course=cls.course, name="Optics", order=2)
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Kinematics", order=1)
        cls.batch = Batch.objects.create(
            course=cls.course, name="Batch A", code="A1", year=2026)
        cls.other_batch = Batch.objects.create(
            course=cls.course, name="Batch B", code="B1", year=2026)

        # `teacher` is course-wide on cls.subject AND additionally carries a
        # row for cls.batch — the shape that must accumulate rather than
        # overwrite.
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=cls.batch,
            is_active=True, role=TeachingAssignment.ROLE_ASSISTANT)
        # `batch_only` covers cls.batch and nothing else. is_teacher_of is
        # therefore false for them on cls.other_batch.
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.batch_only, batch=cls.batch,
            is_active=True, role=TeachingAssignment.ROLE_ASSISTANT)
        # An ACTIVE PRIMARY row with nobody in it. Prod has two of these.
        TeachingAssignment.objects.create(
            subject=cls.bare_subject, teacher=None, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)
        # An ENDED row. is_active=False, so it is not staffing any more.
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.unstaffed_teacher,
            batch=cls.other_batch, is_active=False,
            role=TeachingAssignment.ROLE_SUBSTITUTE)

    def admin_client(self):
        c = APIClient()
        c.force_authenticate(user=self.admin, token={"context": "account"})
        return c

    def teacher_client(self, user=None):
        c = APIClient()
        c.force_authenticate(
            user=user or self.teacher, token={"context": "teacher"})
        return c


class AdminAcademyOptionsTest(AdminAcademyBase):
    def test_requires_staff(self):
        self.assertEqual(APIClient().get(OPTIONS).status_code, 401)
        # A teacher in good standing is not an admin. 403, not an empty list:
        # an empty list would read as "there are no courses".
        self.assertEqual(self.teacher_client().get(OPTIONS).status_code, 403)

    def test_lists_courses_with_subject_counts(self):
        res = self.admin_client().get(OPTIONS)
        self.assertEqual(res.status_code, 200)
        rows = {c["title"]: c for c in res.data["courses"]}
        self.assertIn("Physics", rows)
        self.assertEqual(rows["Physics"]["subject_count"], 2)
        # DRAFT is the model default and is reported, not filtered away.
        self.assertEqual(rows["Physics"]["status"], "DRAFT")

    def test_course_tree_carries_chapters_and_batches(self):
        res = self.admin_client().get(OPTIONS, {"course_id": str(self.course.id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["course"]["title"], "Physics")
        self.assertEqual(
            {b["code"] for b in res.data["batches"]}, {"A1", "B1"})
        by_name = {s["name"]: s for s in res.data["subjects"]}
        self.assertEqual(
            [c["title"] for c in by_name["Mechanics"]["chapters"]],
            ["Kinematics"],
        )
        self.assertEqual(by_name["Optics"]["chapters"], [])

    def test_course_wide_and_batch_rows_accumulate_on_one_teacher(self):
        res = self.admin_client().get(OPTIONS, {"course_id": str(self.course.id)})
        subject = next(
            s for s in res.data["subjects"] if s["name"] == "Mechanics")
        by_id = {t["id"]: t for t in subject["teachers"]}

        # One entry per teacher, not one per assignment row.
        self.assertEqual(len(subject["teachers"]), 2)

        mine = by_id[str(self.teacher.id)]
        self.assertTrue(mine["course_wide"])
        self.assertEqual(mine["batch_ids"], [str(self.batch.id)])
        self.assertEqual(mine["name"], "Tara One")

        # The batch-only teacher must NOT be marked course-wide — that is the
        # flag the console uses to decide whether to offer them for Batch B,
        # and is_teacher_of would refuse.
        theirs = by_id[str(self.batch_only.id)]
        self.assertFalse(theirs["course_wide"])
        self.assertEqual(theirs["batch_ids"], [str(self.batch.id)])

    def test_unusable_accounts_are_not_offered(self):
        # Every option this endpoint lists must be one resolve_content_author
        # will accept. An active TeachingAssignment does NOT imply a usable
        # account: ending the TEACHER role, or deactivating the login, leaves
        # the assignment row exactly where it was.
        quit_role = User.objects.create_user(
            username="exrole", email="exrole@test.com", password="x",
            first_name="Ex", last_name="Role")
        UserRole.objects.create(
            user=quit_role, role=self.teacher_role,
            is_active=False, is_primary=True)
        deactivated = User.objects.create_user(
            username="gone", email="gone@test.com", password="x",
            first_name="Gone", last_name="Away", is_active=False)
        UserRole.objects.create(
            user=deactivated, role=self.teacher_role,
            is_active=True, is_primary=True)
        for u in (quit_role, deactivated):
            TeachingAssignment.objects.create(
                subject=self.subject, teacher=u, batch=self.other_batch,
                is_active=True, role=TeachingAssignment.ROLE_SUBSTITUTE)

        res = self.admin_client().get(OPTIONS, {"course_id": str(self.course.id)})
        subject = next(
            s for s in res.data["subjects"] if s["name"] == "Mechanics")
        ids = {t["id"] for t in subject["teachers"]}
        self.assertNotIn(str(quit_role.id), ids)
        self.assertNotIn(str(deactivated.id), ids)
        # The real staff are still there — this is a filter, not a purge.
        self.assertEqual(ids, {str(self.teacher.id), str(self.batch_only.id)})

    def test_nameless_and_ended_assignments_are_not_staffing(self):
        res = self.admin_client().get(OPTIONS, {"course_id": str(self.course.id)})
        by_name = {s["name"]: s for s in res.data["subjects"]}
        # The subject's only active row has teacher=NULL, so it is unstaffed —
        # reported as such rather than as one blank option.
        self.assertEqual(by_name["Optics"]["teachers"], [])
        # The ended row's holder is absent from the staffed subject too.
        ids = {t["id"] for t in by_name["Mechanics"]["teachers"]}
        self.assertNotIn(str(self.unstaffed_teacher.id), ids)

    def test_bad_course_id_is_a_400_not_a_500(self):
        res = self.admin_client().get(OPTIONS, {"course_id": "not-a-uuid"})
        self.assertEqual(res.status_code, 400)

    def test_unknown_course_is_a_404(self):
        res = self.admin_client().get(
            OPTIONS, {"course_id": "11111111-1111-1111-1111-111111111111"})
        self.assertEqual(res.status_code, 404)


class AdminAcademyResourcesTest(AdminAcademyBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.due = timezone.now() + timedelta(days=7)
        cls.material = StudyMaterial.objects.create(
            subject=cls.subject, chapter=cls.chapter,
            title="Tara's notes", uploaded_by=cls.teacher)
        cls.other_material = StudyMaterial.objects.create(
            subject=cls.subject, title="Bala's notes",
            uploaded_by=cls.batch_only)
        cls.assignment = Assignment.objects.create(
            subject=cls.subject, title="Homework", due_date=cls.due,
            batch=cls.batch, created_by=cls.teacher, is_published=True)

    def test_requires_staff(self):
        self.assertEqual(APIClient().get(RESOURCES).status_code, 401)
        self.assertEqual(self.teacher_client().get(RESOURCES).status_code, 403)

    def test_admin_sees_content_on_subjects_they_do_not_teach(self):
        # The admin holds no TeachingAssignment at all. Under the teacher scope
        # this would be empty, which is the whole bug this view avoids.
        res = self.admin_client().get(RESOURCES)
        self.assertEqual(res.status_code, 200)
        titles = {r["title"] for r in res.data["results"]}
        self.assertEqual(
            titles, {"Tara's notes", "Bala's notes", "Homework"})

    def test_row_shape_matches_the_teacher_hub(self):
        res = self.admin_client().get(RESOURCES, {"type": "material"})
        row = next(r for r in res.data["results"] if r["title"] == "Tara's notes")
        self.assertEqual(row["owner_name"], "Tara One")
        self.assertEqual(row["owner_id"], str(self.teacher.id))
        self.assertEqual(row["status"], "live")
        self.assertEqual(row["subject_name"], "Mechanics")
        self.assertEqual(row["chapter_name"], "Kinematics")
        # is_mine is about the CALLER. The admin authored none of this.
        self.assertFalse(row["is_mine"])

    def test_teacher_id_filters_by_author(self):
        res = self.admin_client().get(
            RESOURCES, {"teacher_id": str(self.batch_only.id)})
        self.assertEqual(
            {r["title"] for r in res.data["results"]}, {"Bala's notes"})

        res = self.admin_client().get(
            RESOURCES, {"teacher_id": str(self.teacher.id)})
        self.assertEqual(
            {r["title"] for r in res.data["results"]},
            {"Tara's notes", "Homework"},
        )

    def test_malformed_teacher_id_does_not_500(self):
        res = self.admin_client().get(RESOURCES, {"teacher_id": "abc"})
        self.assertEqual(res.status_code, 200)
        # Dropped, not treated as "matches nothing" — a filter the server
        # cannot honour must not read as "this teacher has no content".
        self.assertEqual(len(res.data["results"]), 3)

    def test_totals_cover_every_type(self):
        res = self.admin_client().get(RESOURCES, {"type": "material"})
        self.assertEqual(res.data["totals"]["material"], 2)
        self.assertEqual(res.data["totals"]["assignment"], 1)
        self.assertEqual(res.data["totals"]["quiz"], 0)
        self.assertEqual(len(res.data["results"]), 2)

    def test_batch_filter_includes_course_wide_rows(self):
        # Both materials are course-wide (batch NULL); the assignment is scoped
        # to cls.batch. Asking what Batch A has been given must return all
        # three, which is the pre-existing rule this view inherits.
        res = self.admin_client().get(RESOURCES, {"batch_id": str(self.batch.id)})
        self.assertEqual(len(res.data["results"]), 3)

        res = self.admin_client().get(
            RESOURCES, {"batch_id": str(self.other_batch.id)})
        self.assertEqual(
            {r["title"] for r in res.data["results"]},
            {"Tara's notes", "Bala's notes"},
        )
