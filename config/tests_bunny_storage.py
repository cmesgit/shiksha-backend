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
"""
from unittest import mock

from django.test import TestCase, override_settings


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

    def test_a_real_collision_is_now_detected(self):
        """The consequence that matters. `get_available_name` asks `exists()`;
        while that always answered False, two uploads sharing a filename
        overwrote each other on Bunny instead of getting a suffix."""
        seen = {"n": 0}

        def fake_get(url, **kw):
            # First name taken, second free.
            seen["n"] += 1
            return _FakeResponse(200 if seen["n"] == 1 else 404,
                                 {"Content-Length": "9"})

        with mock.patch("config.bunny_storage.requests.get", side_effect=fake_get):
            name = self.storage().get_available_name("content/showcase/photo.jpg")
        self.assertNotEqual(
            name, "content/showcase/photo.jpg",
            "a taken name must be suffixed, not silently reused",
        )
        self.assertTrue(name.startswith("content/showcase/photo"))
        self.assertTrue(name.endswith(".jpg"))

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
