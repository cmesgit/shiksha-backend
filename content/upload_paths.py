"""Where a CMS upload lands, and what it is called when it gets there.

Every CMS ImageField used a bare `upload_to` directory string, so Django's
default storage preserved whatever the admin's file was called. That is how
the homepage ticker came to serve

    /media/content/ticker/WhatsApp_Image_2026-09-11_at_3.58.24_PM.webp

— a public URL carrying an app name, a date and a timestamp, flagged in the
13 Nov site audit. Media is served from a CDN pull zone, so these names are
permanent, world-readable and cached.

`upload_to` takes a callable, which is what these are. They slugify the
stem, keep the real extension, and append a short random suffix so two
admins uploading `logo.png` on the same day do not collide (Django would
otherwise mangle the second into `logo_a8Fq2x.png` itself, which is the same
idea done less predictably).

Existing files are untouched — `upload_to` only runs on write. Renaming what
is already uploaded would break every CMS row pointing at it, and the CDN
would keep serving the old name from cache regardless (see the Bunny purge
note in the infra memory).
"""
import posixpath
import uuid

from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.text import slugify

# Long names get truncated rather than rejected: an over-long path is a
# storage-backend error at upload time, which an editor cannot act on.
MAX_STEM = 60


def _clean_name(filename):
    # Basename FIRST. Splitting on the last dot of "../../etc/passwd" gives
    # an "extension" of "/etc/passwd"; slugify would strip the slashes so it
    # was never a traversal, but the stored name came out as nonsense.
    base = posixpath.basename((filename or "").replace("\\", "/"))

    stem, dot, ext = base.rpartition(".")
    if not dot:                      # no extension at all
        stem, ext = base, ""
    ext = slugify(ext)
    # A real extension is short and alphanumeric. Anything else came from a
    # dot inside the name, so keep the whole thing as the stem.
    if not (1 <= len(ext) <= 8) or not ext.isalnum():
        stem, ext = base, ""

    stem = slugify(stem)[:MAX_STEM] or "image"
    # 6 hex chars: enough that same-day collisions are not a practical
    # concern, short enough to keep the URL readable.
    suffix = uuid.uuid4().hex[:6]
    name = f"{stem}-{suffix}"
    return f"{name}.{ext}" if ext else name


@deconstructible
class cms_upload_to:
    """An `upload_to` callable writing sanitised names into `directory`.

    A CLASS, not a closure: Django's migration serialiser cannot serialise a
    nested function ("Could not find function _upload_to in …"), so the
    factory-returning-a-closure shape fails at `makemigrations`.
    `@deconstructible` lets it round-trip as
    `content.upload_paths.cms_upload_to('content/ticker')`.

    `dated=True` adds a YYYY/MM level, matching what the blog and editor
    fields already did with their `%Y/%m` strings.

    `__eq__` matters: without it every `makemigrations` run would see two
    distinct instances and emit a no-op AlterField for each image column.
    """

    def __init__(self, directory, dated=False):
        self.directory = directory
        self.dated = dated

    def __call__(self, instance, filename):
        parts = [self.directory.strip("/")]
        if self.dated:
            now = timezone.now()
            parts += [f"{now:%Y}", f"{now:%m}"]
        parts.append(_clean_name(filename))
        return posixpath.join(*parts)

    def __eq__(self, other):
        return (
            isinstance(other, cms_upload_to)
            and self.directory == other.directory
            and self.dated == other.dated
        )

    def __hash__(self):
        return hash((self.directory, self.dated))
