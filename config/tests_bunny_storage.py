"""BunnyStorage must never use HEAD.

Bunny Edge Storage does not support HEAD: it answers **401** to a HEAD whether
or not the key is valid and whether or not the object exists, while the same
key on the same URL answers GET 200 (existing) / 404 (missing). Verified
against production storage:

    HEAD existing -> 401        GET existing -> 200 (Content-Length 50009)
    HEAD missing  -> 401        GET missing  -> 404

Both `exists()` and `size()` were written with `requests.head`, so:

  · exists() always returned False, so `get_available_name()` never detected a
    collision and same-named uploads silently overwrote each other — the exact
    thing the comment under get_available_name says must not happen.
  · size() always raised HTTPError, which is what made every PATCH to a
    ShowcaseCourse carrying an image fail on prod (18 of 19 cards): the CMS
    image validator reads `.size` and FullCleanMixin validates the whole row.

These tests fake the HTTP layer rather than talking to Bunny, so they encode
the contract above without needing credentials.

This module also covers a SECOND, unrelated contract further down:
get_available_name must not produce a predictable key. See
BunnyStorageNameTest.
"""
import posixpath
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from config.bunny_storage import BunnyStorage
from config.media_security import is_public


class _FakeResponse:
    def __init__(self, status_code, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Client Error")


@override_settings(
    BUNNY_STORAGE_ZONE="testzone",
    BUNNY_STORAGE_API_KEY="testkey",
    BUNNY_STORAGE_HOSTNAME="sg.storage.bunnycdn.com",
    BUNNY_STORAGE_CDN_HOST="test.b-cdn.net",
)
class BunnyStorageProbeTests(TestCase):
    def storage(self):
        from config.bunny_storage import BunnyStorage
        return BunnyStorage()

    # ── the contract: never HEAD ──────────────────────────────────────

    def test_exists_does_not_use_head(self):
        """HEAD is the bug. If it comes back, this fails."""
        with mock.patch("config.bunny_storage.requests.head") as head, \
             mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(200, {"Content-Length": "12"})):
            self.storage().exists("a/b.jpg")
        head.assert_not_called()

    def test_size_does_not_use_head(self):
        with mock.patch("config.bunny_storage.requests.head") as head, \
             mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(200, {"Content-Length": "12"})):
            self.storage().size("a/b.jpg")
        head.assert_not_called()

    def test_the_probe_streams_and_closes_rather_than_downloading(self):
        """A size check must stay as cheap as the HEAD was meant to be: read
        the headers, never pull the body."""
        resp = _FakeResponse(200, {"Content-Length": "50009"})
        with mock.patch("config.bunny_storage.requests.get", return_value=resp) as get:
            self.storage().size("a/b.jpg")
        self.assertTrue(get.call_args.kwargs.get("stream"),
                        "must pass stream=True or it downloads the object")
        self.assertTrue(resp.closed, "must close the response or the socket leaks")

    # ── exists() ──────────────────────────────────────────────────────

    def test_exists_is_true_for_a_200(self):
        with mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(200, {"Content-Length": "9"})):
            self.assertTrue(self.storage().exists("a/b.jpg"))

    def test_exists_is_false_for_a_404(self):
        with mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(404)):
            self.assertFalse(self.storage().exists("a/nope.jpg"))

    def test_two_uploads_of_one_filename_cannot_overwrite_each_other(self):
        """The consequence that matters, and the mechanism has CHANGED.

        This used to be guarded by exists(): while that always answered False,
        two uploads sharing a filename overwrote each other on Bunny. It is now
        guarded by entropy instead — get_available_name appends 128 random bits
        and never probes at all, so `requests.get` must not even be called.
        Kept because the PROPERTY (no silent overwrite) is what prod cares
        about, whichever mechanism delivers it."""
        with mock.patch("config.bunny_storage.requests.get") as get:
            a = self.storage().get_available_name("content/showcase/photo.jpg")
            b = self.storage().get_available_name("content/showcase/photo.jpg")
        get.assert_not_called()
        self.assertNotEqual(a, "content/showcase/photo.jpg")
        self.assertNotEqual(a, b, "two uploads must never land on one key")
        self.assertTrue(a.startswith("content/showcase/photo"))
        self.assertTrue(a.endswith(".jpg"))

    # ── size() ────────────────────────────────────────────────────────

    def test_size_returns_the_content_length(self):
        with mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(200, {"Content-Length": "50009"})):
            self.assertEqual(self.storage().size("a/b.jpg"), 50009)

    def test_size_raises_oserror_not_httperror_when_missing(self):
        """Callers guard storage reads with OSError (FileSystemStorage raises
        that for a missing file). An HTTPError escaping a `.size` access is
        precisely how this became a 500 on a save path."""
        import requests
        with mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(404)):
            with self.assertRaises(OSError) as cm:
                self.storage().size("a/nope.jpg")
        self.assertNotIsInstance(cm.exception, requests.HTTPError)

    def test_size_is_zero_when_bunny_omits_content_length(self):
        with mock.patch("config.bunny_storage.requests.get",
                        return_value=_FakeResponse(200, {})):
            self.assertEqual(self.storage().size("a/b.jpg"), 0)


# ══════════════════════════════════════════════════════════════════
# Key unpredictability — a separate contract from the HEAD one above.
#
# url() returns a flat, unauthenticated CDN URL and the storage zone has
# no token authentication, so the stored key is the only thing between a
# private document and anyone who can guess its name. `upload_to` is a
# fixed prefix per field, so the whole key used to be
# "teachers/id_proofs/" + whatever the applicant named their file.
#
# The token is hex, not urlsafe: urlsafe's alphabet contains "-", which is
# also the stem separator, and tests that split on it were flaky.
# ══════════════════════════════════════════════════════════════════
class BunnyStorageNameTest(SimpleTestCase):
    def setUp(self):
        self.storage = BunnyStorage()

    def test_stored_name_is_not_the_uploaded_name(self):
        """The whole point: an applicant who uploads aadhaar.pdf must not
        land on the guessable teachers/id_proofs/aadhaar.pdf."""
        got = self.storage.get_available_name("teachers/id_proofs/aadhaar.pdf")
        self.assertNotEqual(got, "teachers/id_proofs/aadhaar.pdf")
        self.assertTrue(got.startswith("teachers/id_proofs/"))
        self.assertTrue(got.endswith(".pdf"))

    def test_same_filename_twice_yields_different_keys(self):
        """Two applicants both uploading "photo.jpg" must not overwrite each
        other — which is what happened while exists() always returned False."""
        a = self.storage.get_available_name("learners/photos/photo.jpg")
        b = self.storage.get_available_name("learners/photos/photo.jpg")
        self.assertNotEqual(a, b)

    def test_entropy_is_present_and_substantial(self):
        """128 bits as 32 hex chars. Guard it so a future edit can't quietly
        shorten the part doing the security work. Asserted as a hex run so
        the check does not depend on how stem and token are joined."""
        got = self.storage.get_available_name("teachers/id_proofs/x.pdf")
        stem = posixpath.splitext(posixpath.basename(got))[0]
        self.assertRegex(stem, r"[0-9a-f]{32}$")

    def test_directory_prefix_is_preserved_exactly(self):
        """media_security.py classifies by DIRECTORY prefix. If this method
        ever altered the prefix, private files would silently reclassify as
        public — so assert the classifier still agrees after renaming."""
        for path, expected_public in [
            ("teachers/id_proofs/front.jpg", False),
            ("teachers/certificates/degree.pdf", False),
            ("teachers/agreements/signed.pdf", False),
            ("scholarship/guardian_docs/2026/09/kyc.pdf", False),
            ("learners/photos/kid.jpg", False),
            ("teachers/skills/images/demo.jpg", True),
        ]:
            with self.subTest(path=path):
                renamed = self.storage.get_available_name(path)
                self.assertEqual(
                    posixpath.dirname(renamed), posixpath.dirname(path)
                )
                self.assertIs(is_public(renamed), expected_public)
                self.assertIs(is_public(path), expected_public)

    def test_extension_preserved_and_lowercased(self):
        got = self.storage.get_available_name("content/blog/Cover.JPG")
        self.assertTrue(got.endswith(".jpg"))

    def test_readable_stem_is_kept_and_slugified(self):
        got = self.storage.get_available_name("content/blog/My Cover Photo!.png")
        base = posixpath.basename(got)
        self.assertTrue(base.startswith("my-cover-photo-"))
        # token_urlsafe is mixed-case and may contain - and _.
        self.assertRegex(base, r"^[a-z0-9\-]+\.png$")

    def test_unsafe_stem_still_produces_a_valid_name(self):
        """A filename of nothing but separators must not yield "-token.ext"
        or an empty basename."""
        got = self.storage.get_available_name("content/blog/___.png")
        base = posixpath.basename(got)
        self.assertFalse(base.startswith("-"))
        self.assertRegex(base, r"^[a-z0-9\-]+\.png$")

    def test_max_length_is_respected_without_truncating_the_token(self):
        """The column limit must give way at the stem, never at the entropy."""
        long_name = "content/blog/" + ("a" * 200) + ".png"
        got = self.storage.get_available_name(long_name, max_length=60)
        self.assertLessEqual(len(got), 60)
        stem = posixpath.splitext(posixpath.basename(got))[0]
        self.assertRegex(stem, r"[0-9a-f]{32}$")

    def test_stem_is_dropped_entirely_when_there_is_no_room_for_it(self):
        """13-char prefix + 32-char token + ".png" = 49. At max_length=49
        there is room for the token but none for the stem, so the stem goes
        and the token survives intact."""
        got = self.storage.get_available_name(
            "content/blog/somename.png", max_length=49
        )
        self.assertEqual(len(got), 49)
        self.assertNotIn("somename", got)
        self.assertRegex(got, r"^content/blog/[0-9a-f]{32}\.png$")

    def test_token_is_never_sacrificed_to_fit_max_length(self):
        """When even the prefix + token does not fit, the name is returned
        over-length ON PURPOSE. Storing a guessable key to satisfy a column
        limit is the failure mode this whole method exists to prevent — let
        the DB complain instead."""
        got = self.storage.get_available_name(
            "content/blog/somename.png", max_length=20
        )
        self.assertGreater(len(got), 20)
        self.assertRegex(got, r"[0-9a-f]{32}\.png$")

    def test_traversal_cannot_escape_the_upload_prefix(self):
        """`..` segments are dropped rather than resolved, so a filename
        containing them stays inside its upload_to prefix. That prefix is
        exactly what media_security.py classifies by, so escaping it is what
        would turn a private document public — assert the classification, not
        just the string."""
        got = self.storage.get_available_name("teachers/id_proofs/../../etc/passwd.pdf")
        self.assertNotIn("..", got)
        self.assertTrue(got.startswith("teachers/id_proofs/"))
        self.assertFalse(is_public(got))
