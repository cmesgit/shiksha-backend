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
from courses.models import Course, Batch
from enrollments.models import Enrollment, EnrollmentRequest


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


class FreeEnrollBatchTest(TestCase):
    """A student can choose their own batch (Morning/Afternoon/etc) at
    free-enroll time instead of relying on an admin to assign one later."""
    URL = "/api/enrollments/free-enroll/"

    @classmethod
    def setUpTestData(cls):
        student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="stu", email="stu@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.account, role=student_role, is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="Stu", full_name="Stu Dent", is_default=True)
        cls.course = Course.objects.create(title="Physics")
        cls.other_course = Course.objects.create(title="Chemistry")
        cls.morning = Batch.objects.create(course=cls.course, name="Morning", code="M1")
        cls.full_batch = Batch.objects.create(course=cls.course, name="Evening", code="E1", capacity=1)
        cls.other_course_batch = Batch.objects.create(course=cls.other_course, name="Morning", code="OM1")
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            batch=cls.full_batch, status=Enrollment.STATUS_ACTIVE,
        )

    def client_as(self, profile):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        return c

    def test_free_enroll_with_valid_batch_sets_batch(self):
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.other_course.id), "batch": str(self.other_course_batch.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["batch"]["id"], str(self.other_course_batch.id))
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.other_course)
        self.assertEqual(enrollment.batch_id, self.other_course_batch.id)

    def test_free_enroll_without_batch_leaves_null(self):
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.other_course.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.data["batch"])
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.other_course)
        self.assertIsNone(enrollment.batch_id)

    def test_free_enroll_batch_from_wrong_course_rejected(self):
        res = self.client_as(self.profile).post(
            self.URL,
            {"course": str(self.other_course.id), "batch": str(self.morning.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            Enrollment.objects.filter(learner_profile=self.profile, course=self.other_course).exists()
        )

    def test_free_enroll_full_batch_rejected(self):
        # full_batch already has this profile's own enrollment counted against
        # its capacity=1, so a second (different) learner enrolling into it
        # should be rejected as full.
        other_role = Role.objects.get(name="STUDENT")
        other_account = User.objects.create_user(
            username="stu2", email="stu2@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=other_account, role=other_role, is_active=True, is_primary=True)
        other_profile = LearnerProfile.objects.create(
            account=other_account, display_name="Stu2", full_name="Stu Two", is_default=True)
        c = APIClient()
        c.force_authenticate(
            user=other_account,
            token={"context": "learner", "active_profile": str(other_profile.id)},
        )
        res = c.post(
            self.URL, {"course": str(self.course.id), "batch": str(self.full_batch.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_re_calling_free_enroll_sets_batch_on_already_unbatched_enrollment(self):
        # First call with no batch (already-enrolled, batch=None case).
        self.client_as(self.profile).post(
            self.URL, {"course": str(self.other_course.id)}, format="json",
        )
        # Second call supplies a batch — should now attach it.
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.other_course.id), "batch": str(self.other_course_batch.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.other_course)
        self.assertEqual(enrollment.batch_id, self.other_course_batch.id)

    def test_re_calling_free_enroll_does_not_override_existing_batch(self):
        # This profile already has an ACTIVE enrollment in `course` with
        # `full_batch` assigned. Calling free-enroll again for the same
        # course with a DIFFERENT batch must not silently move them.
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.course.id), "batch": str(self.morning.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.course)
        self.assertEqual(enrollment.batch_id, self.full_batch.id)


class SelectEnrollmentBatchTest(TestCase):
    """An already-enrolled, unbatched student can self-select a batch."""
    URL = "/api/enrollments/select-batch/"

    @classmethod
    def setUpTestData(cls):
        student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(username="stu3", email="stu3@test.com", password="x")
        UserRole.objects.create(user=cls.account, role=student_role, is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="Stu3", full_name="Stu Three", is_default=True)
        cls.course = Course.objects.create(title="Biology")
        cls.batch = Batch.objects.create(course=cls.course, name="Morning", code="M1")
        cls.unbatched_enrollment = Enrollment.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Enrollment.STATUS_ACTIVE,
        )

    def client_as(self, profile):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        return c

    def test_select_batch_sets_unbatched_enrollment(self):
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.course.id), "batch": str(self.batch.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.unbatched_enrollment.refresh_from_db()
        self.assertEqual(self.unbatched_enrollment.batch_id, self.batch.id)

    def test_select_batch_rejected_if_already_has_batch(self):
        self.unbatched_enrollment.batch = self.batch
        self.unbatched_enrollment.save(update_fields=["batch"])
        other_batch = Batch.objects.create(course=self.course, name="Evening", code="E1")
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(self.course.id), "batch": str(other_batch.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.unbatched_enrollment.refresh_from_db()
        self.assertEqual(self.unbatched_enrollment.batch_id, self.batch.id)

    def test_select_batch_rejected_if_not_enrolled(self):
        other_course = Course.objects.create(title="Chemistry")
        other_batch = Batch.objects.create(course=other_course, name="Morning", code="M1")
        res = self.client_as(self.profile).post(
            self.URL, {"course": str(other_course.id), "batch": str(other_batch.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


class EnrollmentRequestBatchPreferenceTest(TestCase):
    """The manual-UPI enrollment request can carry a student's batch
    preference, and admin approval defaults to it when left unspecified."""

    @classmethod
    def setUpTestData(cls):
        student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(username="stu4", email="stu4@test.com", password="x")
        UserRole.objects.create(user=cls.account, role=student_role, is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="Stu4", full_name="Stu Four", is_default=True)
        cls.admin = User.objects.create_user(
            username="admin1", email="admin1@test.com", password="x", is_staff=True)
        cls.course = Course.objects.create(title="History")
        cls.batch = Batch.objects.create(course=cls.course, name="Morning", code="M1")

    def student_client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _make_request(self, batch=None):
        req = EnrollmentRequest.objects.create(
            user=self.account, learner_profile=self.profile, course=self.course,
            amount_paid=1000, utr_number="UTR-1", payment_date=date(2026, 1, 1),
            batch=batch,
        )
        return req

    def test_admin_approve_without_batch_honors_student_preference(self):
        req = self._make_request(batch=self.batch)
        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        res = admin_client.post(
            f"/api/enrollments/admin/requests/{req.id}/action/",
            {"action": "approve"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.course)
        self.assertEqual(enrollment.batch_id, self.batch.id)

    def test_admin_approve_explicit_batch_overrides_preference(self):
        other_batch = Batch.objects.create(course=self.course, name="Evening", code="E1")
        req = self._make_request(batch=self.batch)
        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        res = admin_client.post(
            f"/api/enrollments/admin/requests/{req.id}/action/",
            {"action": "approve", "batch": str(other_batch.id)}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        enrollment = Enrollment.objects.get(learner_profile=self.profile, course=self.course)
        self.assertEqual(enrollment.batch_id, other_batch.id)

    def test_admin_list_surfaces_requested_batch(self):
        self._make_request(batch=self.batch)
        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        res = admin_client.get("/api/enrollments/admin/requests/")
        rows = res.data if isinstance(res.data, list) else res.data.get("results", [])
        self.assertEqual(rows[0]["requested_batch_name"], "Morning")
