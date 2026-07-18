"""
materials — teacher write endpoints must require teacher CONTEXT (not just an
authenticated session), and delete is restricted to the uploading teacher.
Previously upload/delete were only IsAuthenticated → any account (incl. a child
on a shared device) could upload or delete study material.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course, Subject, Chapter
from materials.models import StudyMaterial


class MaterialsDeleteGateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher = User.objects.create_user(username="t1", email="t1@test.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=cls.teacher_role, is_active=True, is_primary=True)
        cls.other_teacher = User.objects.create_user(username="t2", email="t2@test.com", password="x")
        UserRole.objects.create(user=cls.other_teacher, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.child_account = User.objects.create_user(username="kid", email="kid@test.com", password="x")
        UserRole.objects.create(user=cls.child_account, role=cls.student_role, is_active=True, is_primary=True)
        cls.child = LearnerProfile.objects.create(account=cls.child_account, display_name="Kid", is_default=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Kinematics")
        cls.material = StudyMaterial.objects.create(chapter=cls.chapter, title="Notes",
                                                    uploaded_by=cls.teacher)

    def url(self):
        return f"/api/materials/materials/{self.material.id}/delete/"

    def client_with(self, user, context):
        c = APIClient()
        token = {"context": context}
        if context == "learner":
            token["active_profile"] = str(self.child.id)
        c.force_authenticate(user=user, token=token)
        return c

    def test_learner_context_cannot_delete(self):
        res = self.client_with(self.child_account, "learner").delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_other_teacher_cannot_delete(self):
        res = self.client_with(self.other_teacher, "teacher").delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_uploading_teacher_can_delete(self):
        res = self.client_with(self.teacher, "teacher").delete(self.url())
        self.assertIn(res.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(StudyMaterial.objects.filter(id=self.material.id).exists())
