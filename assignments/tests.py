"""
Regression cover: AssignmentDetailView and CourseAssignmentsView used to
branch on the account-level `user.has_role(Role.TEACHER)` instead of the
request's actual context. A dual-role account (STUDENT + an active TEACHER
role — this platform explicitly supports holding several active roles at
once) hit the teacher-ownership branch even while acting as a learner in a
learner-context token, 403'ing its own enrolled subject's assignment with
"Not assigned to this subject." Both views now use
accounts.permissions._in_teacher_context(), which additionally checks the
token's `context` claim, matching every other teacher-gated view in this
codebase.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course, Subject, Chapter
from enrollments.models import Subscription
from assignments.models import Assignment


class DualRoleStudentAssignmentAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        # A dual-role account: active STUDENT role AND an active (but
        # unrelated) TEACHER role — e.g. an approved faculty member who
        # also has their own learner profile.
        cls.account = User.objects.create_user(
            username="dual_role", email="dual_role@test.com", password="x",
            is_verified=True,
        )
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="STUDENT"), is_active=True, is_primary=True)
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=False)

        cls.profile = LearnerProfile.objects.create(account=cls.account, display_name="Learner side", is_default=True)

        cls.course = Course.objects.create(title="Physics Demo")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Laws of Motion", order=0)

        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )

        cls.assignment = Assignment.objects.create(
            chapter=cls.chapter, title="Problem set 1", due_date=now + timedelta(days=7),
        )

    def client_in_learner_context(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def test_assignment_detail_accessible_to_dual_role_student_in_learner_context(self):
        c = self.client_in_learner_context()
        r = c.get(f"/api/assignments/{self.assignment.id}/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_course_assignments_list_accessible_to_dual_role_student_in_learner_context(self):
        c = self.client_in_learner_context()
        r = c.get(f"/api/assignments/courses/{self.course.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        titles = [row["title"] for row in r.data]
        self.assertIn("Problem set 1", titles)
