"""Cover for an admin creating an assignment on a teacher's behalf.

`Assignment` had no admin API surface of any kind — no `is_staff` branch
anywhere in the app — so this is the whole of the admin path's behaviour.

The property that has to hold is the batch one. Assignments are gated on
`is_teacher_of`, which a course-wide (batch=NULL) assignment row satisfies for
every batch but a batch-scoped row satisfies only for its own batch. So a
teacher can be genuinely staffed on the subject and still be the wrong answer
for the batch the admin picked, and the refusal has to say which batch — that
error message was rewritten once already because "You are not assigned to this
subject" is false and unactionable for exactly this case.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole
from assignments.models import Assignment
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment

CREATE = "/api/assignments/teacher/create/"


class AdminAssignmentCreateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")

        cls.admin = User.objects.create_user(
            username="boss", email="boss@test.com", password="x",
            is_staff=True, first_name="Ada", last_name="Boss")
        # Course-wide on the subject: valid for every batch.
        cls.wide = User.objects.create_user(
            username="t1", email="t1@test.com", password="x",
            first_name="Tara", last_name="One")
        # Batch A only: valid for A, refused for B.
        cls.batch_only = User.objects.create_user(
            username="t2", email="t2@test.com", password="x",
            first_name="Bala", last_name="Two")
        for u in (cls.wide, cls.batch_only):
            UserRole.objects.create(
                user=u, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Kinematics")
        cls.batch_a = Batch.objects.create(
            course=cls.course, name="Batch A", code="A1", year=2026)
        cls.batch_b = Batch.objects.create(
            course=cls.course, name="Batch B", code="B1", year=2026)

        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.wide, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.batch_only, batch=cls.batch_a,
            is_active=True, role=TeachingAssignment.ROLE_ASSISTANT)

    def client_for(self, user, context="teacher"):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": context})
        return c

    def admin_client(self):
        return self.client_for(self.admin, context="account")

    def payload(self, **over):
        body = {
            "subject_id": str(self.subject.id),
            "batch_id": str(self.batch_a.id),
            "title": "Worksheet 1",
            "description": "Do it",
            "due_date": (timezone.now() + timedelta(days=5)).isoformat(),
            "max_marks": 20,
        }
        body.update(over)
        return body

    # ------------------------------------------------------------------

    def test_admin_create_is_owned_by_the_chosen_teacher(self):
        res = self.admin_client().post(
            CREATE, self.payload(teacher_id=str(self.wide.id)), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        a = Assignment.objects.get(id=res.data["id"])
        self.assertEqual(a.created_by_id, self.wide.id)
        self.assertEqual(a.batch_id, self.batch_a.id)
        self.assertTrue(a.is_published)

    def test_admin_create_shows_up_in_that_teachers_hub_as_theirs(self):
        self.admin_client().post(
            CREATE,
            self.payload(teacher_id=str(self.wide.id), title="Handover HW"),
            format="json",
        )
        res = self.client_for(self.wide).get(
            "/api/dashboard/teacher/resources/", {"mine": "true"})
        rows = [r for r in res.data["results"] if r["title"] == "Handover HW"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_mine"])
        self.assertEqual(rows[0]["owner_name"], "Tara One")

    def test_admin_can_save_a_draft(self):
        res = self.admin_client().post(
            CREATE,
            self.payload(teacher_id=str(self.wide.id), is_published=False),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertFalse(Assignment.objects.get(id=res.data["id"]).is_published)

    def test_batch_scoped_teacher_is_accepted_for_their_own_batch(self):
        res = self.admin_client().post(
            CREATE,
            self.payload(teacher_id=str(self.batch_only.id)),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_batch_scoped_teacher_is_refused_for_another_batch(self):
        res = self.admin_client().post(
            CREATE,
            self.payload(
                teacher_id=str(self.batch_only.id),
                batch_id=str(self.batch_b.id),
                title="Wrong batch",
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        # The message must name the BATCH — and, on the admin path, the
        # TEACHER. "Pick a batch you teach" is advice an admin cannot follow.
        self.assertIn("Batch B", str(res.data))
        self.assertIn("Bala Two", str(res.data))
        self.assertNotIn("you teach", str(res.data))
        self.assertFalse(
            Assignment.objects.filter(title="Wrong batch").exists())

    def test_admin_must_name_a_teacher(self):
        res = self.admin_client().post(CREATE, self.payload(), format="json")
        # 400, not 403: a pure admin has no teacher profile to switch to, so
        # "Switch to your teacher profile" would be advice they cannot follow.
        # Naming somebody is a form error.
        self.assertEqual(res.status_code, 400)
        self.assertIn("teacher_id", res.data)
        self.assertFalse(Assignment.objects.exists())

    def test_a_teacher_cannot_file_under_someone_else(self):
        res = self.client_for(self.wide).post(
            CREATE,
            self.payload(teacher_id=str(self.batch_only.id), title="Framed"),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("admin", str(res.data).lower())

    def test_malformed_teacher_id_is_a_400_not_a_500(self):
        res = self.admin_client().post(
            CREATE, self.payload(teacher_id="nope"), format="json")
        self.assertEqual(res.status_code, 400)

    def test_admin_can_mint_a_chapter_credited_to_the_teacher(self):
        res = self.admin_client().post(
            CREATE,
            self.payload(
                teacher_id=str(self.wide.id),
                custom_chapter="Rotational Motion",
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        chapter = Chapter.objects.get(title="Rotational Motion")
        self.assertEqual(chapter.created_by_id, self.wide.id)

    # ------------------------------------------------------------------
    # regressions
    # ------------------------------------------------------------------

    def test_teacher_create_is_unchanged(self):
        res = self.client_for(self.wide).post(
            CREATE, self.payload(title="My own HW"), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            Assignment.objects.get(title="My own HW").created_by_id,
            self.wide.id,
        )

    def test_a_student_and_an_anonymous_caller_are_refused(self):
        student = User.objects.create_user(
            username="s1", email="s1@test.com", password="x")
        self.assertEqual(
            self.client_for(student, context="learner").post(
                CREATE, self.payload(title="Student HW"), format="json",
            ).status_code,
            403,
        )
        # Naming a real teacher does not help them: teacher_id is admin-only.
        self.assertEqual(
            self.client_for(student, context="learner").post(
                CREATE,
                self.payload(teacher_id=str(self.wide.id), title="Student HW 2"),
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            APIClient().post(CREATE, self.payload(), format="json").status_code,
            401,
        )
        self.assertFalse(Assignment.objects.exists())

    def test_learner_context_on_a_teacher_account_is_still_refused(self):
        res = self.client_for(self.wide, context="learner").post(
            CREATE, self.payload(title="Shared device"), format="json")
        self.assertEqual(res.status_code, 403)

    def test_admin_still_cannot_edit_or_delete(self):
        # Deliberately NOT built in this pass: the edit and delete views run
        # require_teacher_context plus _assert_teacher_owns_assignment. The
        # owning teacher can do both. Asserted so that a later change to those
        # views is a decision rather than a surprise.
        created = self.admin_client().post(
            CREATE, self.payload(teacher_id=str(self.wide.id)), format="json")
        aid = created.data["id"]
        self.assertEqual(
            self.admin_client().patch(
                f"/api/assignments/teacher/{aid}/edit/",
                {"title": "Renamed"}, format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.admin_client().delete(
                f"/api/assignments/teacher/{aid}/delete/").status_code,
            403,
        )
