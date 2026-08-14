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

    def exists(self, name):
        r = requests.head(self._storage_url(name), headers=self._headers(), timeout=self.TIMEOUT)
        return r.status_code == 200

    def delete(self, name):
        requests.delete(self._storage_url(name), headers=self._headers(), timeout=self.TIMEOUT)

    def url(self, name):
        host = settings.BUNNY_STORAGE_CDN_HOST.rstrip("/")
        return f"https://{host}/{name}"

    def size(self, name):
        r = requests.head(self._storage_url(name), headers=self._headers(), timeout=self.TIMEOUT)
        r.raise_for_status()
        return int(r.headers.get("Content-Length", 0))

    # get_available_name is intentionally NOT overridden — the base Storage
    # implementation already calls exists() (implemented above) and appends
    # a suffix on collision, same as local FileSystemStorage. Two different
    # uploads that happen to share a filename (e.g. two categories both
    # getting a phone photo named "photo.jpg") must not silently overwrite
    # each other on Bunny.
