"""
#4 — MyEnrollmentRequestListView must scope billing history to the active
learner profile (not mix all profiles on the account), and the serializer must
label each row with the learner.
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course
from enrollments.models import EnrollmentRequest


class MyEnrollmentRequestScopeTest(TestCase):
    URL = "/api/enrollments/requests/mine/"

    @classmethod
    def setUpTestData(cls):
        student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(username="parent", email="p@test.com", password="x")
        UserRole.objects.create(user=cls.account, role=student_role, is_active=True, is_primary=True)
        cls.child_a = LearnerProfile.objects.create(account=cls.account, display_name="Aria",
                                                     full_name="Aria Kid", is_default=True)
        cls.child_b = LearnerProfile.objects.create(account=cls.account, display_name="Bina",
                                                     full_name="Bina Kid", is_default=False)
        cls.course = Course.objects.create(title="Algebra")

        def mk(profile):
            return EnrollmentRequest.objects.create(
                user=cls.account, learner_profile=profile, course=cls.course,
                amount_paid=1000, utr_number=f"UTR-{profile.display_name}",
                payment_date=date(2026, 1, 1),
            )
        cls.req_a = mk(cls.child_a)
        cls.req_b = mk(cls.child_b)

    def client_as(self, profile):
        c = APIClient()
        c.force_authenticate(user=self.account,
                             token={"context": "learner", "active_profile": str(profile.id)})
        return c

    def _rows(self, res):
        return res.data if isinstance(res.data, list) else res.data.get("results", [])

    def test_child_sees_only_own_request(self):
        res = self.client_as(self.child_a).get(self.URL)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = self._rows(res)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["learner_profile_id"], str(self.child_a.id))
        self.assertEqual(rows[0]["learner_name"], "Aria Kid")

    def test_sibling_sees_only_their_own(self):
        res = self.client_as(self.child_b).get(self.URL)
        rows = self._rows(res)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["learner_name"], "Bina Kid")
