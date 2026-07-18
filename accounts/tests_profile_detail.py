"""
ProfileDetailView full-detail GET/PATCH by id — a parent can view AND edit any
child's academic + guardian fields from Manage profile (not just the active one).
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, Role, UserRole, LearnerProfile


class ProfileDetailFullFieldsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="parent", email="parent@test.com", password="x",
        )
        UserRole.objects.create(
            user=cls.account, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.child = LearnerProfile.objects.create(
            account=cls.account, display_name="Aria", is_default=True,
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def url(self):
        return f"/api/accounts/profiles/{self.child.id}/"

    def test_get_returns_academic_and_guardian_fields(self):
        self.child.current_class = "10"
        self.child.father_name = "Ravi"
        self.child.save()
        res = self.client_for(self.account).get(self.url())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["current_class"], "10")
        self.assertEqual(res.data["father_name"], "Ravi")
        # academic + guardian keys are present even when empty
        for key in ("stream", "board", "school_name", "mother_phone",
                    "parent_guardian_email", "highest_education", "student_id"):
            self.assertIn(key, res.data)

    def test_patch_sets_full_fields(self):
        res = self.client_for(self.account).patch(
            self.url(),
            {
                "current_class": "11", "stream": "science", "board": "cbse",
                "school_name": "Green Valley High",
                "father_name": "Ravi", "father_phone": "9990001111",
                "guardian_name": "Meena", "parent_guardian_email": "m@ex.com",
                "currently_studying": "yes",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["stream"], "science")
        self.child.refresh_from_db()
        self.assertEqual(self.child.current_class, "11")
        self.assertEqual(self.child.school_name, "Green Valley High")
        self.assertEqual(self.child.father_phone, "9990001111")
        self.assertEqual(self.child.parent_guardian_email, "m@ex.com")

    def test_patch_invalid_choice_rejected(self):
        res = self.client_for(self.account).patch(
            self.url(), {"current_class": "99"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_class", res.data)
        self.child.refresh_from_db()
        self.assertEqual(self.child.current_class, "")

    def test_patch_max_length_enforced(self):
        res = self.client_for(self.account).patch(
            self.url(), {"school_name": "x" * 300}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("school_name", res.data)
