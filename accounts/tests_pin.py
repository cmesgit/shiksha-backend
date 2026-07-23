"""
Tests for PIN re-auth + profile-delete re-auth (outstanding #2).

A profile's switch-PIN can only be set / changed / reset / removed with the
ACCOUNT password, and a profile can only be deleted with the account password.
This closes the bypass where any session on the account (e.g. a child on a
shared device) could strip a parent's PIN or delete profiles, and provides the
"forgot PIN" reset path (account password, no old PIN needed).
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from datetime import timedelta

from django.utils import timezone

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course
from enrollments.models import Subscription


class PinReauthTest(TestCase):
    PASSWORD = "s3cret-pass"

    @classmethod
    def setUpTestData(cls):
        cls.student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="parent", email="parent@test.com", password=cls.PASSWORD,
        )
        UserRole.objects.create(
            user=cls.account, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.child_a = LearnerProfile.objects.create(
            account=cls.account, display_name="Aria", is_default=True,
        )
        cls.child_b = LearnerProfile.objects.create(
            account=cls.account, display_name="Bina", is_default=False,
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    # ---- set / change / reset PIN --------------------------------------

    def test_set_pin_without_password_rejected(self):
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "1234"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get("code"), "password_required")
        self.child_a.refresh_from_db()
        self.assertFalse(self.child_a.has_pin())

    def test_set_pin_wrong_password_rejected(self):
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "1234", "password": "nope"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get("code"), "bad_password")
        self.child_a.refresh_from_db()
        self.assertFalse(self.child_a.has_pin())

    def test_set_pin_correct_password(self):
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "1234", "password": self.PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data["requires_pin"])
        self.child_a.refresh_from_db()
        self.assertTrue(self.child_a.check_pin("1234"))

    def test_forgot_pin_reset_needs_only_account_password(self):
        # Existing PIN set directly on the model.
        self.child_a.set_pin("1111")
        self.child_a.save(update_fields=["pin"])
        # Reset to a new PIN WITHOUT knowing the old one — account password only.
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "9999", "password": self.PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.child_a.refresh_from_db()
        self.assertTrue(self.child_a.check_pin("9999"))

    def test_remove_pin_with_password(self):
        self.child_a.set_pin("1111")
        self.child_a.save(update_fields=["pin"])
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "", "password": self.PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["requires_pin"])
        self.child_a.refresh_from_db()
        self.assertFalse(self.child_a.has_pin())

    def test_pin_digit_validation_still_enforced(self):
        res = self.client_for(self.account).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(self.child_a.id), "pin": "abc", "password": self.PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pin", res.data)

    # ---- patch bypass closed -------------------------------------------

    def test_patch_cannot_change_pin(self):
        res = self.client_for(self.account).patch(
            f"/api/accounts/profiles/{self.child_a.id}/",
            {"pin": "1234"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get("code"), "pin_requires_password")
        self.child_a.refresh_from_db()
        self.assertFalse(self.child_a.has_pin())

    # ---- delete re-auth -------------------------------------------------

    def test_delete_without_password_rejected(self):
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/", format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.child_b.refresh_from_db()
        self.assertTrue(self.child_b.is_active)

    def test_delete_wrong_password_rejected(self):
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/",
            {"password": "nope"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.child_b.refresh_from_db()
        self.assertTrue(self.child_b.is_active)

    def test_delete_correct_password(self):
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/",
            {"password": self.PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.child_b.refresh_from_db()
        self.assertFalse(self.child_b.is_active)


class DeleteBlockedByActiveSubscriptionTest(TestCase):
    """#6 — a profile with a live paid subscription must not be deletable:
    get_active_profile only resolves is_active=True profiles, so soft-deleting
    would strand that paid access with no reactivation path."""
    PASSWORD = "s3cret-pass"

    @classmethod
    def setUpTestData(cls):
        cls.student_role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="parent2", email="parent2@test.com", password=cls.PASSWORD,
        )
        UserRole.objects.create(
            user=cls.account, role=cls.student_role, is_active=True, is_primary=True,
        )
        cls.child_a = LearnerProfile.objects.create(
            account=cls.account, display_name="Aria", is_default=True,
        )
        cls.child_b = LearnerProfile.objects.create(
            account=cls.account, display_name="Bina", is_default=False,
        )
        cls.course = Course.objects.create(title="Algebra")

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def _sub(self, profile, status=Subscription.STATUS_ACTIVE, expires_in_days=30):
        return Subscription.objects.create(
            user=self.account, learner_profile=profile, course=self.course,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=expires_in_days),
            status=status,
        )

    def test_delete_blocked_when_subscription_active(self):
        self._sub(self.child_b)
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/",
            {"password": self.PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get("code"), "active_subscription")
        self.assertIn("Algebra", res.data.get("courses", []))
        self.child_b.refresh_from_db()
        self.assertTrue(self.child_b.is_active)

    def test_delete_allowed_when_subscription_expired(self):
        self._sub(self.child_b, expires_in_days=-1)
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/",
            {"password": self.PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.child_b.refresh_from_db()
        self.assertFalse(self.child_b.is_active)

    def test_delete_allowed_when_subscription_cancelled(self):
        self._sub(self.child_b, status=Subscription.STATUS_CANCELLED)
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_b.id}/",
            {"password": self.PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_delete_allowed_for_sibling_without_subscription(self):
        # child_b has an active subscription, but deleting child_a (no sub) is fine.
        self._sub(self.child_b)
        res = self.client_for(self.account).delete(
            f"/api/accounts/profiles/{self.child_a.id}/",
            {"password": self.PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
