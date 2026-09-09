"""
Tests for Google ID-token verification (OAuth groundwork).

The network call to Google is mocked throughout — what is under test is our
claim handling, not google-auth's cryptography. The one thing these tests
guard most carefully is that we never verify a token without pinning the
audience, because that is the difference between "Google says this is Alice"
and "some other website's Google login says this is Alice".

There is no endpoint yet; that is the next phase.
"""
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase, override_settings

from accounts.models import GoogleIdentity, User
from accounts.oauth_google import (
    GoogleAuthError,
    google_client_id,
    verify_google_id_token,
)

CLIENT_ID = "123456789.apps.googleusercontent.com"
VERIFY_PATH = "google.oauth2.id_token.verify_oauth2_token"


def claims(**overrides):
    base = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-1",
        "email": "alice@gmail.com",
        "email_verified": True,
        "name": "Alice Example",
        "picture": "https://example.com/a.png",
    }
    base.update(overrides)
    return base


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
class VerifyGoogleIdTokenTest(TestCase):

    def test_valid_token_returns_normalised_claims(self):
        with patch(VERIFY_PATH, return_value=claims()):
            result = verify_google_id_token("a.credential")
        self.assertEqual(result, {
            "sub": "google-sub-1",
            "email": "alice@gmail.com",
            "name": "Alice Example",
            "picture": "https://example.com/a.png",
        })

    def test_audience_is_pinned_to_our_client_id(self):
        """The security assertion this whole module exists for.

        A token minted for a different application verifies perfectly well on
        signature alone. Passing our client id is what makes google-auth reject
        it. If this ever regresses, any site with a Google login can mint
        credentials that authenticate here.
        """
        with patch(VERIFY_PATH, return_value=claims()) as verify:
            verify_google_id_token("a.credential")
        args, _kwargs = verify.call_args
        self.assertIn(CLIENT_ID, args)

    def test_email_is_lowercased_and_stripped(self):
        with patch(VERIFY_PATH, return_value=claims(email="  Alice@GMail.com ")):
            result = verify_google_id_token("a.credential")
        self.assertEqual(result["email"], "alice@gmail.com")

    def test_unverified_email_is_refused(self):
        with patch(VERIFY_PATH, return_value=claims(email_verified=False)):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "email_unverified")

    def test_missing_email_verified_claim_is_refused(self):
        payload = claims()
        del payload["email_verified"]
        with patch(VERIFY_PATH, return_value=payload):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "email_unverified")

    def test_foreign_issuer_is_refused(self):
        with patch(VERIFY_PATH, return_value=claims(iss="https://evil.example")):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "invalid_issuer")

    def test_both_google_issuers_are_accepted(self):
        for issuer in ("accounts.google.com", "https://accounts.google.com"):
            with patch(VERIFY_PATH, return_value=claims(iss=issuer)):
                self.assertEqual(
                    verify_google_id_token("a.credential")["sub"], "google-sub-1"
                )

    def test_bad_signature_or_expiry_is_refused(self):
        with patch(VERIFY_PATH, side_effect=ValueError("Token expired")):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "invalid_token")

    def test_missing_sub_is_refused(self):
        with patch(VERIFY_PATH, return_value=claims(sub=None)):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "incomplete_claims")

    def test_missing_email_is_refused(self):
        with patch(VERIFY_PATH, return_value=claims(email="")):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "incomplete_claims")

    def test_empty_credential_is_refused(self):
        for value in ("", None):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token(value)
            self.assertEqual(ctx.exception.code, "missing_credential")

    def test_error_messages_never_leak_token_material(self):
        with patch(VERIFY_PATH, side_effect=ValueError("bad sig for eyJhbGciOi.SECRET")):
            with self.assertRaises(GoogleAuthError) as ctx:
                verify_google_id_token("eyJhbGciOi.SECRET")
        self.assertNotIn("SECRET", ctx.exception.detail)


class UnconfiguredServerTest(TestCase):
    """With no client id, refuse — never verify with an unchecked audience."""

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_refuses_when_not_configured(self):
        with self.assertRaises(GoogleAuthError) as ctx:
            verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "not_configured")

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_does_not_even_call_google_when_not_configured(self):
        with patch(VERIFY_PATH) as verify:
            with self.assertRaises(GoogleAuthError):
                verify_google_id_token("a.credential")
        verify.assert_not_called()

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="   ")
    def test_whitespace_client_id_counts_as_unconfigured(self):
        self.assertEqual(google_client_id(), "")
        with self.assertRaises(GoogleAuthError) as ctx:
            verify_google_id_token("a.credential")
        self.assertEqual(ctx.exception.code, "not_configured")


class GoogleIdentityModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", email="alice@test.com", password="x-pass-1234",
        )

    def test_sub_is_unique_across_accounts(self):
        other = User.objects.create_user(
            username="bob", email="bob@test.com", password="x-pass-1234",
        )
        GoogleIdentity.objects.create(
            user=self.user, sub="dup-sub", email_at_link="alice@gmail.com",
        )
        with self.assertRaises(IntegrityError):
            GoogleIdentity.objects.create(
                user=other, sub="dup-sub", email_at_link="bob@gmail.com",
            )

    def test_one_identity_per_user(self):
        GoogleIdentity.objects.create(
            user=self.user, sub="sub-1", email_at_link="alice@gmail.com",
        )
        with self.assertRaises(IntegrityError):
            GoogleIdentity.objects.create(
                user=self.user, sub="sub-2", email_at_link="alice2@gmail.com",
            )

    def test_reverse_accessor(self):
        GoogleIdentity.objects.create(
            user=self.user, sub="sub-1", email_at_link="alice@gmail.com",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.google_identity.sub, "sub-1")


class GoogleOAuthFlagTest(TestCase):
    def test_flag_defaults_off(self):
        from global_settings.models import GlobalSettings
        self.assertFalse(GlobalSettings.load().google_oauth_enabled)
