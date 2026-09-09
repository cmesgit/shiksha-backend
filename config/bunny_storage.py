"""
config/bunny_storage.py — Django Storage backend for Bunny.net Edge Storage.

Every CMS image upload (blog covers, showcase cards, SkillDev categories/
marketing/expert photos/course covers) previously used Django's default
FileSystemStorage — writing to local disk, which doesn't survive a redeploy
and isn't served through the CDN. Video already talks to Bunny directly via
plain `requests` calls (see skills/views_intro_video.py) rather than through
Django's storage abstraction; this gives images the same treatment through
the one interface ImageField actually understands.

Wired in as STORAGES["default"] only when BUNNY_STORAGE_ZONE and
BUNNY_STORAGE_API_KEY are both set (see settings_base.py) — local/test
environments without real Bunny credentials keep using local disk unchanged.
"""
import posixpath
import re
import secrets

import requests
from django.conf import settings
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class BunnyStorage(Storage):
    # (connect, read) — a hung connection to Bunny must not tie up an ASGI
    # worker indefinitely on a single-box deploy.
    TIMEOUT = (5, 30)

    def _headers(self):
        return {"AccessKey": settings.BUNNY_STORAGE_API_KEY}

    def _storage_url(self, name):
        return f"https://{settings.BUNNY_STORAGE_HOSTNAME}/{settings.BUNNY_STORAGE_ZONE}/{name}"

    def _save(self, name, content):
        content.seek(0)
        r = requests.put(
            self._storage_url(name),
            data=content.read(),
            headers={**self._headers(), "Content-Type": "application/octet-stream"},
            timeout=self.TIMEOUT,
        )
        r.raise_for_status()
        return name

    def _open(self, name, mode="rb"):
        from django.core.files.base import ContentFile
        r = requests.get(self._storage_url(name), headers=self._headers(), timeout=self.TIMEOUT)
        r.raise_for_status()
        return ContentFile(r.content, name=name)

    # ⚠ Bunny Edge Storage DOES NOT SUPPORT HEAD. It answers **401** to a HEAD
    # regardless of whether the key is valid or the object exists, while the
    # same key on the same URL answers GET 200 (existing) / 404 (missing).
    # Verified against prod storage: HEAD existing -> 401, HEAD missing -> 401,
    # GET existing -> 200 (Content-Length 50009), GET missing -> 404.
    #
    # Both methods below were written with `requests.head`, so:
    #   · exists() ALWAYS returned False  → get_available_name() never saw a
    #     collision, so same-named uploads silently overwrote each other on
    #     Bunny. That is no longer what protects against an overwrite —
    #     get_available_name now appends 128 random bits and does not probe at
    #     all (see its own note below) — but exists() is still public API and
    #     still has callers, so the fix stands on its own.
    #   · size() ALWAYS raised HTTPError  → any code touching `.size` on a
    #     Bunny-backed file 500'd. That is what made every PATCH to a
    #     ShowcaseCourse carrying an image fail on prod (18 of 19 cards),
    #     because FullCleanMixin validates the whole row and the CMS image
    #     validator reads `.size`.
    #
    # `stream=True` + `close()` keeps this as cheap as the HEAD was meant to
    # be: headers are read, the body is never downloaded.
    def _probe(self, name):
        """(status_code, content_length) for an object, without downloading it."""
        r = requests.get(
            self._storage_url(name),
            headers=self._headers(),
            timeout=self.TIMEOUT,
            stream=True,
        )
        try:
            return r.status_code, r.headers.get("Content-Length")
        finally:
            r.close()

    def exists(self, name):
        status, _ = self._probe(name)
        return status == 200

    def delete(self, name):
        requests.delete(self._storage_url(name), headers=self._headers(), timeout=self.TIMEOUT)

    def url(self, name):
        host = settings.BUNNY_STORAGE_CDN_HOST.rstrip("/")
        return f"https://{host}/{name}"

    def size(self, name):
        status, length = self._probe(name)
        if status != 200:
            # Mirror FileSystemStorage, which raises OSError for a missing
            # file. Deliberately NOT requests.HTTPError: callers reasonably
            # guard storage reads with OSError, and an HTTPError leaking out
            # of a `.size` access is how this became a 500 on a save path.
            raise OSError(f"Bunny storage returned {status} for {name!r}")
        return int(length or 0)

    # ⚠ SECURITY: the stored key MUST NOT be predictable from the upload.
    #
    # This used to defer to base Storage.get_available_name, which keeps the
    # caller's filename and only appends a suffix ON COLLISION. Combined with
    # url() below — a flat, unauthenticated CDN URL — that made every private
    # document reachable by guessing its name. `upload_to` is a fixed prefix
    # per field, so a faculty applicant uploading "aadhaar.pdf" landed at
    # exactly `teachers/id_proofs/aadhaar.pdf`, and the same held for
    # learners/photos/ (children's pictures), scholarship/guardian_docs/ and
    # enrollment_receipts/. Verified against prod: the storage zone has no
    # token authentication, so a private prefix answers 404 for a missing key
    # rather than 403 — i.e. any key that exists is served to anyone.
    #
    # Appending 128 bits of entropy (32 hex chars) makes the key
    # unguessable, which is the
    # only defence that works while url() stays unsigned. It is NOT a
    # replacement for turning on Bunny token authentication and signing
    # private URLs — and it does NOT retroactively protect the files already
    # stored under their original names. Both remain outstanding.
    #
    # The readable stem is kept (slugified, truncated) because it costs
    # nothing against a random suffix and keeps CDN URLs debuggable and
    # SEO-sane for the public CMS images that share this backend.
    #
    # exists() is deliberately NOT consulted any more: with a 128-bit token a
    # collision is not a real event, and skipping it removes a network
    # round-trip from every single upload.
    STEM_MAX = 40

    def get_available_name(self, name, max_length=None):
        dirname, basename = posixpath.split(name)
        stem, ext = posixpath.splitext(basename)

        # `..` segments are DROPPED, not resolved. The caller's filename can
        # contain slashes, so posixpath.split leaves them in dirname — and
        # normpath() would happily walk out of the upload_to prefix
        # ("teachers/id_proofs/../../etc" -> "teachers/etc"), which lands the
        # file outside the tree media_security.py classifies it by. Dropping
        # instead keeps it under the intended prefix. Django validates this
        # upstream too; this is the second lock, not the only one.
        dirname = "/".join(s for s in dirname.split("/") if s not in ("", ".", ".."))

        ext = ext.lower()
        stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[: self.STEM_MAX]
        # token_hex, NOT token_urlsafe: urlsafe's alphabet includes "-",
        # which is also the stem/token separator here — that made the
        # boundary ambiguous and any test that split on it flaky.
        token = secrets.token_hex(16)

        def build(s):
            return posixpath.join(dirname, f"{s}-{token}{ext}" if s else f"{token}{ext}")

        candidate = build(stem)
        if max_length is not None and len(candidate) > max_length:
            # Give room back from the stem first, and drop it entirely if the
            # prefix plus the token already fills the column. Never truncate
            # the token — that is the part doing the security work. The -1 is
            # the "-" that build() inserts between stem and token.
            #
            # If build("") alone still exceeds max_length the prefix itself
            # does not fit; return it anyway rather than shortening the token,
            # and let the DB raise instead of storing a guessable key.
            room = max_length - len(build("")) - 1
            candidate = build(stem[:room].strip("-") if room > 0 else "")
        return candidate
