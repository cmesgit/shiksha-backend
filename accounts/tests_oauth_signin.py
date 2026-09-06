"""
Tests for POST /api/accounts/oauth/google/.

Google's verification is mocked at our module boundary — `verify_google_id_token`
has its own dedicated tests in tests_oauth_google.py. What is under test here
is account resolution: who a given Google identity ends up signed in as, and
who it must NOT end up signed in as.

The takeover test (`test_unverified_shell_account_is_reclaimed_not_linked`) is
the one to read first; it is why linking is restricted to verified accounts.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import GoogleIdentity, LearnerProfile, Role, User, UserSession
from global_settings.models import GlobalSettings

URL = "/api/accounts/oauth/google/"
VERIFY = "accounts.oauth_views.verify_google_id_token"

CLAIMS = {
    "sub": "google-sub-1",
    "email": "alice@gmail.com",
    "name": "Alice Example",
    "picture": "https://example.com/a.png",
}


def enable_google():
    gs = GlobalSettings.load()
    gs.google_oauth_enabled = True
    gs.save(update_fields=["google_oauth_enabled"])


class GoogleSignInFlagTest(TestCase):
    def test_refused_while_flag_is_off(self):
        with patch(VERIFY, return_value=CLAIMS) as verify:
            res = APIClient().post(URL, {"credential": "c"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["code"], "disabled")
        # Must short-circuit before spending an outbound call to Google.
        verify.assert_not_called()


class GoogleSignInTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        enable_google()

    def _post(self, claims=None, **body):
        payload = {"credential": "a.credential"}
        payload.update(body)
        with patch(VERIFY, return_value=claims or CLAIMS):
            return self.client.post(URL, payload, format="json")

    # ── new account ───────────────────────────────────────────────────────

    def test_unknown_email_without_consent_returns_no_account(self):
        res = self._post()
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        body = res.json()
        self.assertEqual(body["code"], "no_account")
        self.assertEqual(body["email"], "alice@gmail.com")
        self.assertEqual(body["name"], "Alice Example")
        self.assertFalse(User.objects.filter(email="alice@gmail.com").exists())

    def test_consent_creates_a_verified_account_and_signs_in(self):
        res = self._post(terms_accepted=True)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["context"], "learner")
        self.assertIn("access", res.cookies)

        user = User.objects.get(email="alice@gmail.com")
        # Google asserted email_verified, so no verification mail is needed.
        self.assertTrue(user.is_verified)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.has_role(Role.STUDENT))
        self.assertTrue(body["needs_password"])

        profile = user.learner_profiles.get()
        self.assertEqual(profile.display_name, "Alice Example")
        self.assertEqual(profile.relationship, LearnerProfile.RELATIONSHIP_SELF)
        self.assertTrue(profile.is_default)

        self.assertEqual(GoogleIdentity.objects.get(user=user).sub, "google-sub-1")

    def test_second_sign_in_reuses_the_identity(self):
        self._post(terms_accepted=True)
        res = self._post()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.json()["created"])
        self.assertEqual(User.objects.filter(email="alice@gmail.com").count(), 1)
        self.assertEqual(GoogleIdentity.objects.count(), 1)

    def test_each_sign_in_opens_one_session(self):
        self._post(terms_accepted=True)
        self._post()
        user = User.objects.get(email="alice@gmail.com")
        self.assertEqual(UserSession.objects.filter(user=user).count(), 2)

    # ── linking to an existing account ────────────────────────────────────

    def test_links_to_an_existing_verified_account(self):
        existing = User.objects.create_user(
            username="alice", email="alice@gmail.com", password="pw-1234-abcd",
        )
        User.objects.filter(pk=existing.pk).update(is_verified=True)

        res = self._post()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.json()["created"])
        self.assertEqual(User.objects.filter(email="alice@gmail.com").count(), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, existing.id)
        # They already had a password — don't ask for another.
        self.assertFalse(res.json()["needs_password"])

    def test_unverified_shell_account_is_reclaimed_not_linked(self):
        """The takeover case.

        An attacker registers the victim's address (registration cannot prove
        mailbox ownership) and waits. If Google sign-in linked by email, the
        victim would be handed an account whose password the attacker knows.
        The unverified shell must be discarded instead.
        """
        attacker_shell = User.objects.create_user(
            username="attacker", email="alice@gmail.com", password="attacker-pw-1",
        )
        self.assertFalse(attacker_shell.is_verified)

        res = self._post(terms_accepted=True)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.json()["created"])

        self.assertFalse(User.objects.filter(pk=attacker_shell.pk).exists())
        user = User.objects.get(email="alice@gmail.com")
        self.assertNotEqual(user.pk, attacker_shell.pk)
        # The attacker's password must not survive onto the new account.
        self.assertFalse(user.check_password("attacker-pw-1"))
        self.assertFalse(user.has_usable_password())

    def test_sub_wins_over_email(self):
        """Google is the authority on which account a `sub` belongs to.

        If someone changes the email on their Google account to one that
        matches a DIFFERENT ShikshaCom account, the existing link must hold.
        """
        original = User.objects.create_user(
            username="orig", email="original@test.com", password="pw-1234-abcd",
        )
        User.objects.filter(pk=original.pk).update(is_verified=True)
        GoogleIdentity.objects.create(
            user=original, sub="google-sub-1", email_at_link="original@test.com",
        )
        decoy = User.objects.create_user(
            username="decoy", email="alice@gmail.com", password="pw-1234-abcd",
        )
        User.objects.filter(pk=decoy.pk).update(is_verified=True)

        res = self._post()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(GoogleIdentity.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, original.id)

    # ── response shape matches LoginView ──────────────────────────────────

    def test_multi_profile_account_returns_account_context(self):
        existing = User.objects.create_user(
            username="parent", email="alice@gmail.com", password="pw-1234-abcd",
        )
        User.objects.filter(pk=existing.pk).update(is_verified=True)
        LearnerProfile.objects.create(
            account=existing, display_name="Me",
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_default=True,
        )
        LearnerProfile.objects.create(
            account=existing, display_name="Kid",
            relationship=LearnerProfile.RELATIONSHIP_DEPENDENT,
        )
        res = self._post()
        self.assertEqual(res.json()["context"], "account")
        self.assertEqual(len(res.json()["profiles"]), 2)

    # ── verification failures ─────────────────────────────────────────────

    def test_rejected_token_is_a_400(self):
        from accounts.oauth_google import GoogleAuthError
        with patch(VERIFY, side_effect=GoogleAuthError("invalid_token", "nope")):
            res = self.client.post(URL, {"credential": "bad"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["code"], "invalid_token")
        self.assertNotIn("access", res.cookies)

    def test_unconfigured_server_is_a_503(self):
        from accounts.oauth_google import GoogleAuthError
        with patch(VERIFY, side_effect=GoogleAuthError("not_configured", "nope")):
            res = self.client.post(URL, {"credential": "c"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_failed_verification_creates_nothing(self):
        from accounts.oauth_google import GoogleAuthError
        with patch(VERIFY, side_effect=GoogleAuthError("invalid_token", "nope")):
            self.client.post(URL, {"credential": "bad", "terms_accepted": True},
                             format="json")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(GoogleIdentity.objects.count(), 0)


@override_settings(GOOGLE_OAUTH_CLIENT_ID="cid.apps.googleusercontent.com")
class GoogleSignInEndToEndTest(TestCase):
    """Through the real verifier, with only Google's network call mocked."""

    def setUp(self):
        enable_google()

    def test_real_verifier_path_signs_in(self):
        google_claims = {
            "iss": "https://accounts.google.com",
            "aud": "cid.apps.googleusercontent.com",
            "sub": "sub-e2e",
            "email": "e2e@gmail.com",
            "email_verified": True,
            "name": "E2E User",
        }
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=google_claims):
            res = APIClient().post(
                URL, {"credential": "c", "terms_accepted": True}, format="json",
            )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.get(email="e2e@gmail.com").is_verified)

    def test_real_verifier_refuses_unverified_google_email(self):
        google_claims = {
            "iss": "https://accounts.google.com",
            "aud": "cid.apps.googleusercontent.com",
            "sub": "sub-e2e",
            "email": "e2e@gmail.com",
            "email_verified": False,
        }
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=google_claims):
            res = APIClient().post(
                URL, {"credential": "c", "terms_accepted": True}, format="json",
            )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["code"], "email_unverified")
        self.assertEqual(User.objects.count(), 0)
