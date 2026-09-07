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
    #     Bunny — the exact thing the note under get_available_name promises
    #     cannot happen.
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

    # get_available_name is intentionally NOT overridden — the base Storage
    # implementation already calls exists() (implemented above) and appends
    # a suffix on collision, same as local FileSystemStorage. Two different
    # uploads that happen to share a filename (e.g. two categories both
    # getting a phone photo named "photo.jpg") must not silently overwrite
    # each other on Bunny.
