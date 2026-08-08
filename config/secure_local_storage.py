# Wraps Django's default FileSystemStorage so private media files resolve
# to an authenticated URL (config.media_views.secure_media_view) instead of
# the raw /media/ path nginx used to serve with zero auth. Files still land
# on local disk exactly as before — only .url() changes, which means every
# existing `some_field.url` call site across every serializer picks this up
# automatically with no other code changes.
#
# The secure URL is mounted under /api/ — the only path nginx already
# proxies to Django (see nginx location blocks) — not under MEDIA_URL
# ("/media/"), which nginx serves as a static alias straight off disk and
# never forwards to the app at all.
#
# Wired in as STORAGES["default"] only when Bunny Storage credentials are
# NOT set (see settings_base.py) — this repo's actual dev/prod environment
# today has no Bunny Storage credentials configured, so this is the storage
# genuinely in effect, not BunnyStorage.
from django.core.files.storage import FileSystemStorage

from .media_security import is_public

SECURE_MEDIA_PREFIX = "/api/media/secure/"


class SecureLocalStorage(FileSystemStorage):
    def url(self, name):
        if is_public(name):
            return super().url(name)
        return f"{SECURE_MEDIA_PREFIX}{name}"
