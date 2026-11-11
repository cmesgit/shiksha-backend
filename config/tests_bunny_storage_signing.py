"""Storage URL signing — the groundwork for closing the public-media hole.

The hole: `BunnyStorage.url()` hands out a bare public CDN URL for every
FileField, so `config/media_security.py`'s deny-by-default rules never run in
production. Teacher government-ID scans, learner photos, guardian KYC
documents and payment receipts are readable by anyone with the path, and the
paths are enumerable.

These tests pin two things, and the second matters more than the first:

  1. the signature is Bunny's documented shape, and
  2. **nothing changes until it is deliberately switched on.** A security
     patch that silently alters every media URL on the platform is how you
     turn a leak into an outage. With no token key configured the output must
     be byte-for-byte what it was before.

Signing is NOT verified against a live Bunny zone — none has token
authentication enabled. A wrong byte fails closed (403), so the risk is a
broken download rather than an ungated one, but see
docs/security/private-media-exposure.md for what is still unproven.
"""

import hashlib
from base64 import b64encode

from django.test import SimpleTestCase, override_settings

from config.bunny_signing import (
    bunny_storage_token,
    bunny_storage_url,
    is_private_storage_path,
    storage_token_configured,
)

HOST = "shiksha-media.test.b-cdn.net"
KEY = "unit-test-storage-token-key-not-a-real-credential"
PRIVATE = ("teachers/", "learners/", "scholarship/")


class PrivatePathClassificationTests(SimpleTestCase):
    @override_settings(BUNNY_PRIVATE_PREFIXES=PRIVATE)
    def test_private_prefixes_are_recognised(self):
        for name in (
            "teachers/id_proofs/19_id_proof_front.png",
            "learners/photos/3_photo.jpg",
            "scholarship/guardian_docs/2026/09/doc.pdf",
        ):
            self.assertTrue(is_private_storage_path(name), name)

    @override_settings(BUNNY_PRIVATE_PREFIXES=PRIVATE)
    def test_public_content_is_not_swept_in(self):
        """Blog covers and course art are public on purpose."""
        for name in (
            "content/blog/2026/07/class-9-science-chapter-7.jpg",
            "courses/icons/neet.svg",
        ):
            self.assertFalse(is_private_storage_path(name), name)

    @override_settings(BUNNY_PRIVATE_PREFIXES=PRIVATE)
    def test_a_leading_slash_does_not_defeat_the_match(self):
        self.assertTrue(is_private_storage_path("/teachers/id_proofs/a.png"))


@override_settings(
    BUNNY_STORAGE_TOKEN_KEY=KEY,
    BUNNY_PRIVATE_PREFIXES=PRIVATE,
    BUNNY_STORAGE_CDN_HOST=HOST,
)
class SigningTests(SimpleTestCase):
    def test_token_matches_bunnys_documented_formula(self):
        """Base64(SHA256_RAW(key + path + expires)), made URL-safe."""
        path, expires = "/teachers/id_proofs/19_id_proof_front.png", 1800000000

        expected = (
            b64encode(hashlib.sha256(f"{KEY}{path}{expires}".encode()).digest())
            .decode()
            .replace("+", "-")
            .replace("/", "_")
            .replace("=", "")
        )
        self.assertEqual(bunny_storage_token(path, expires), expected)

    def test_token_is_url_safe(self):
        """A '+' or '/' in the token would be mangled in a query string."""
        for i in range(60):
            token = bunny_storage_token(f"/teachers/f{i}.png", 1800000000 + i)
            self.assertNotIn("+", token)
            self.assertNotIn("/", token)
            self.assertNotIn("=", token)

    def test_the_path_is_signed_so_a_token_cannot_be_moved(self):
        """Swapping the path under a stolen token must invalidate it."""
        self.assertNotEqual(
            bunny_storage_token("/teachers/id_proofs/19_front.png", 1800000000),
            bunny_storage_token("/teachers/id_proofs/20_front.png", 1800000000),
        )

    def test_expiry_is_signed_too(self):
        self.assertNotEqual(
            bunny_storage_token("/teachers/a.png", 1800000000),
            bunny_storage_token("/teachers/a.png", 1800003600),
        )

    def test_private_url_is_signed_and_reports_itself(self):
        url, expires, signed = bunny_storage_url(
            "teachers/id_proofs/19_id_proof_front.png", host=HOST
        )
        self.assertTrue(signed)
        self.assertIsNotNone(expires)
        self.assertIn("token=", url)
        self.assertIn(f"expires={expires}", url)
        self.assertTrue(url.startswith(f"https://{HOST}/teachers/"))

    def test_public_url_is_left_alone_even_with_a_key_set(self):
        url, expires, signed = bunny_storage_url(
            "content/blog/2026/07/cover.jpg", host=HOST
        )
        self.assertFalse(signed)
        self.assertIsNone(expires)
        self.assertEqual(url, f"https://{HOST}/content/blog/2026/07/cover.jpg")


class InertUntilConfiguredTests(SimpleTestCase):
    """The property that makes this safe to deploy ahead of the Bunny change."""

    @override_settings(
        BUNNY_STORAGE_TOKEN_KEY="",
        BUNNY_PRIVATE_PREFIXES=PRIVATE,
        BUNNY_STORAGE_CDN_HOST=HOST,
    )
    def test_no_key_means_the_old_url_byte_for_byte(self):
        for name in (
            "teachers/id_proofs/19_id_proof_front.png",
            "content/blog/2026/07/cover.jpg",
        ):
            url, expires, signed = bunny_storage_url(name, host=HOST)
            self.assertEqual(url, f"https://{HOST}/{name}")
            self.assertFalse(signed)
            self.assertIsNone(expires)

    @override_settings(BUNNY_STORAGE_TOKEN_KEY="")
    def test_configuration_state_is_reportable(self):
        """Callers can surface 'not gated' rather than implying it is."""
        self.assertFalse(storage_token_configured())

    @override_settings(BUNNY_STORAGE_TOKEN_KEY=KEY)
    def test_configuration_state_is_true_once_keyed(self):
        self.assertTrue(storage_token_configured())
