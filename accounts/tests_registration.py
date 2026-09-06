"""
Tests for the account-first registration path (Phase 1).

Covers `POST /api/accounts/register/` and the auto-login that now happens when
the verification link is clicked. The older role-first `POST /signup/` is
untouched by this phase and keeps its own coverage.

See design_handoff_account_model/BUILD_GUIDE.md.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import (
    CURRENT_TERMS_VERSION,
    EmailVerificationToken,
    LearnerProfile,
    Role,
    User,
    UserSession,
)

REGISTER_URL = "/api/accounts/register/"
VERIFY_URL   = "/api/accounts/verify-email/"
GOOD_PASSWORD = "corr3ct-horse-batt3ry"


def error_code(response):
    """DRF coerces every value in a ValidationError dict to a list, so a
    `code` raised as a string arrives as `["email_taken"]`. That is the shape
    the rest of this codebase already produces (see `verify_account_password`
    in auth_flow.py), so callers unwrap rather than the serializer changing
    shape. Normalise it here so the assertions read plainly."""
    value = response.json().get("code")
    return value[0] if isinstance(value, list) else value


class RegisterViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Every register call sends mail; the transport is not under test.
        self.mail = patch("accounts.registration.send_gmail").start()
        self.addCleanup(patch.stopall)

    def _register(self, **overrides):
        payload = {
            "email": "new@test.com",
            "password": GOOD_PASSWORD,
            "terms_accepted": True,
        }
        payload.update(overrides)
        return self.client.post(REGISTER_URL, payload, format="json")

    # ── happy path ────────────────────────────────────────────────────────

    def test_register_creates_account_profile_and_role(self):
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.json()["email_sent"])

        user = User.objects.get(email="new@test.com")
        self.assertFalse(user.is_verified)
        self.assertEqual(user.accepted_terms_version, CURRENT_TERMS_VERSION)
        self.assertIsNotNone(user.terms_accepted_at)

        profiles = user.learner_profiles.filter(is_active=True)
        self.assertEqual(profiles.count(), 1)
        profile = profiles.first()
        self.assertEqual(profile.relationship, LearnerProfile.RELATIONSHIP_SELF)
        self.assertTrue(profile.is_default)
        self.assertFalse(profile.has_pin())

        self.assertTrue(user.has_role(Role.STUDENT))
        self.assertEqual(EmailVerificationToken.objects.filter(user=user).count(), 1)

    def test_register_asks_for_nothing_beyond_the_three_fields(self):
        """No role, no teacher_type, no profiles[] — the whole point."""
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_mail_failure_still_creates_the_account(self):
        self.mail.side_effect = RuntimeError("smtp down")
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(res.json()["email_sent"])
        self.assertTrue(User.objects.filter(email="new@test.com").exists())

    # ── validation ────────────────────────────────────────────────────────

    def test_terms_are_required(self):
        res = self._register(terms_accepted=False)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email="new@test.com").exists())

    def test_weak_password_rejected(self):
        res = self._register(password="123")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email="new@test.com").exists())

    def test_existing_verified_email_rejected(self):
        User.objects.create_user(
            username="taken", email="new@test.com", password=GOOD_PASSWORD,
        )
        User.objects.filter(email="new@test.com").update(is_verified=True)
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(error_code(res), "email_taken")

    def test_recent_unverified_email_is_not_silently_replaced(self):
        """A link may already be sitting in their inbox — don't invalidate it."""
        User.objects.create_user(
            username="pending", email="new@test.com", password=GOOD_PASSWORD,
        )
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(error_code(res), "email_unverified")

    def test_abandoned_unverified_signup_is_reaped(self):
        stale = User.objects.create_user(
            username="stale", email="new@test.com", password=GOOD_PASSWORD,
        )
        User.objects.filter(pk=stale.pk).update(
            date_joined=timezone.now() - timedelta(hours=25),
        )
        res = self._register()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.filter(email="new@test.com").count(), 1)
        self.assertNotEqual(User.objects.get(email="new@test.com").pk, stale.pk)

    def test_email_is_normalised_to_lowercase(self):
        res = self._register(email="  MiXeD@Test.COM  ")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email="mixed@test.com").exists())


class VerifyEmailAutoLoginTest(TestCase):
    """Clicking the verification link must land the person signed in."""

    def setUp(self):
        self.client = APIClient()
        patch("accounts.registration.send_gmail").start()
        self.addCleanup(patch.stopall)
        self.client.post(
            REGISTER_URL,
            {"email": "new@test.com", "password": GOOD_PASSWORD,
             "terms_accepted": True},
            format="json",
        )
        self.user  = User.objects.get(email="new@test.com")
        self.token = EmailVerificationToken.objects.get(user=self.user)

    def _verify(self):
        return self.client.get(VERIFY_URL, {"token": str(self.token.token)})

    def test_verification_signs_the_user_in(self):
        res = self._verify()
        self.assertEqual(res.status_code, status.HTTP_302_FOUND)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)
        self.assertIsNotNone(self.user.verified_at)

        self.assertIn("access", res.cookies)
        self.assertIn("refresh", res.cookies)
        self.assertTrue(res.cookies["access"].value)
        # A fresh account has one PIN-free profile and no teacher identity,
        # so it auto-selects straight into learner context.
        self.assertIn("context=learner", res.url)

    def test_auto_login_opens_exactly_one_session(self):
        self._verify()
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_token_is_single_use(self):
        self.assertEqual(self._verify().status_code, status.HTTP_302_FOUND)
        second = self._verify()
        self.assertIn("status=failed", second.url)
        self.assertNotIn("access", second.cookies)

    def test_bad_token_sets_no_cookies(self):
        res = self.client.get(VERIFY_URL, {"token": "00000000-0000-0000-0000-000000000000"})
        self.assertIn("status=failed", res.url)
        self.assertNotIn("access", res.cookies)

    def test_missing_token_sets_no_cookies(self):
        res = self.client.get(VERIFY_URL)
        self.assertIn("status=failed", res.url)
        self.assertNotIn("access", res.cookies)

    def test_expired_token_sets_no_cookies(self):
        EmailVerificationToken.objects.filter(pk=self.token.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        res = self._verify()
        self.assertIn("status=failed", res.url)
        self.assertNotIn("access", res.cookies)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_verified)

    def test_multi_profile_account_lands_on_the_picker(self):
        """Auto-login must respect the same auto-select rule login uses."""
        LearnerProfile.objects.create(
            account=self.user, display_name="Kid",
            relationship=LearnerProfile.RELATIONSHIP_DEPENDENT,
        )
        res = self._verify()
        self.assertIn("context=account", res.url)
        self.assertIn("access", res.cookies)

    def test_pinned_single_profile_lands_on_the_picker(self):
        profile = self.user.learner_profiles.first()
        profile.set_pin("4321")
        profile.save()
        res = self._verify()
        self.assertIn("context=account", res.url)

    def test_verification_still_succeeds_if_session_minting_fails(self):
        """Verification is already committed — never turn that into a retry
        the user cannot perform, since the token is gone."""
        with patch("accounts.auth_flow.open_session", side_effect=RuntimeError("boom")):
            res = self._verify()
        self.assertIn("status=success", res.url)
        self.assertNotIn("context=", res.url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)


class RegisteredUserCanLogInTest(TestCase):
    """End-to-end: register → verify → the normal password login still works."""

    def test_full_round_trip(self):
        client = APIClient()
        patch("accounts.registration.send_gmail").start()
        self.addCleanup(patch.stopall)

        client.post(
            REGISTER_URL,
            {"email": "rt@test.com", "password": GOOD_PASSWORD,
             "terms_accepted": True},
            format="json",
        )
        user = User.objects.get(email="rt@test.com")
        token = EmailVerificationToken.objects.get(user=user)
        client.get(VERIFY_URL, {"token": str(token.token)})

        fresh = APIClient()
        res = fresh.post(
            "/api/accounts/login/",
            {"email": "rt@test.com", "password": GOOD_PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["context"], "learner")
        self.assertTrue(res.json()["auto_selected"])
