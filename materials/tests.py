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


class MaterialsSubscriptionGateTest(TestCase):
    """ChapterMaterials/SubjectMaterials/StudyMaterialDetail were
    IsAuthenticated with no subscription or enrollment check at all — any
    authenticated account (a free signup, no subscription) could enumerate
    subject/chapter/material UUIDs and read (and download, via file_url)
    every paid course's material. Regression cover for the fix requiring a
    teaching assignment or an active subscription."""

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone
        from datetime import timedelta
        from enrollments.models import Subscription
        from courses.models import TeachingAssignment, Batch

        teacher_role = Role.objects.create(name="TEACHER")
        student_role = Role.objects.create(name="STUDENT")

        cls.teacher = User.objects.create_user(username="mg_t", email="mg_t@test.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=teacher_role, is_active=True, is_primary=True)

        cls.paying_account = User.objects.create_user(username="mg_pay", email="mg_pay@test.com", password="x")
        UserRole.objects.create(user=cls.paying_account, role=student_role, is_active=True, is_primary=True)
        cls.paying_profile = LearnerProfile.objects.create(
            account=cls.paying_account, display_name="Payer", is_default=True
        )

        cls.freeloader_account = User.objects.create_user(username="mg_free", email="mg_free@test.com", password="x")
        UserRole.objects.create(user=cls.freeloader_account, role=student_role, is_active=True, is_primary=True)
        cls.freeloader_profile = LearnerProfile.objects.create(
            account=cls.freeloader_account, display_name="Freeloader", is_default=True
        )

        cls.course = Course.objects.create(title="Paid Course")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Kinematics")
        cls.material = StudyMaterial.objects.create(
            chapter=cls.chapter, title="Paid Notes", uploaded_by=cls.teacher
        )

        cls.batch = Batch.objects.create(course=cls.course, name="Batch 1", code="B1")
        TeachingAssignment.objects.create(
            batch=cls.batch, subject=cls.subject, teacher=cls.teacher,
        )
        Subscription.objects.create(
            user=cls.paying_account, learner_profile=cls.paying_profile,
            course=cls.course, status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )

    def client_with(self, user, context, profile=None):
        c = APIClient()
        token = {"context": context}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        c.force_authenticate(user=user, token=token)
        return c

    def test_freeloader_cannot_read_subject_materials(self):
        c = self.client_with(self.freeloader_account, "learner", self.freeloader_profile)
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_freeloader_cannot_read_material_detail(self):
        c = self.client_with(self.freeloader_account, "learner", self.freeloader_profile)
        res = c.get(f"/api/materials/materials/{self.material.id}/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_paying_student_can_read_subject_materials(self):
        c = self.client_with(self.paying_account, "learner", self.paying_profile)
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_assigned_teacher_can_read_subject_materials(self):
        c = self.client_with(self.teacher, "teacher")
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_unassigned_teacher_cannot_upload_to_this_chapter(self):
        other_teacher = User.objects.create_user(username="mg_t2", email="mg_t2@test.com", password="x")
        UserRole.objects.create(
            user=other_teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        c = self.client_with(other_teacher, "teacher")
        res = c.post(
            "/api/materials/materials/upload/",
            {"chapter_id": str(self.chapter.id), "title": "Sneaky", "file_ids": []},
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_claim_another_teachers_uploaded_file(self):
        """MaterialFile.uploaded_by existed on the model but was never set at
        upload time nor checked at attach time — any teacher could attach
        (steal) a file another teacher had just uploaded, by guessing or
        reading its UUID."""
        from materials.models import MaterialFile
        import io

        other_teacher = User.objects.create_user(username="mg_t3", email="mg_t3@test.com", password="x")
        UserRole.objects.create(
            user=other_teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        stolen = MaterialFile.objects.create(
            file="study_materials/stolen.txt", material=None, uploaded_by=other_teacher,
        )
        c = self.client_with(self.teacher, "teacher")
        res = c.post(
            "/api/materials/materials/upload/",
            {"chapter_id": str(self.chapter.id), "title": "Grab", "file_ids": [str(stolen.id)]},
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
