# PLACEMENT: backend/content/validators.py
#
# Guard-rails for CMS image uploads.
#
# Why this exists
# ---------------
# The About-hero image field accepted a 4096x4096, 12.7 MB PNG straight off
# somebody's phone. Nothing rejected it, nothing resized it, and because the
# hero never read that field the upload sat there invisible — so the only
# feedback the editor got was "my image didn't appear". Had the field been
# wired, every visitor to /about would have downloaded 12.7 MB.
#
# These validators run on the model field, so they apply to every route into
# storage: the admin CMS API, Django admin, and any management command that
# assigns to the field.
#
# Deliberately NOT a resizer. Silently re-encoding an editor's upload is its
# own surprise (it changes crops and quality without telling anyone); refusing
# it with a message that says what to do is more honest. If automatic
# downscaling is wanted later it belongs in the serializer, next to the upload,
# not hidden in a field validator.

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

# A full-bleed CMS banner renders at most ~1600 CSS px, so 2560 leaves room for
# 1.5x displays without paying for a print-resolution original.
MAX_DIMENSION = 2560
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
# Pillow's format name -> what an editor would recognise in a file dialog.
_FRIENDLY = {"JPEG": "JPG", "PNG": "PNG", "WEBP": "WebP"}


@deconstructible
class CmsImageValidator:
    """Reject CMS images that are too large, too big, or the wrong format.

    Kept as a deconstructible class rather than three separate functions so a
    field only needs one entry and migrations stay readable.
    """

    def __init__(self, max_dimension=MAX_DIMENSION, max_bytes=MAX_BYTES):
        self.max_dimension = max_dimension
        self.max_bytes = max_bytes

    def __call__(self, value):
        errors = []

        size = getattr(value, "size", None)
        if size and size > self.max_bytes:
            errors.append(
                f"File is {size / 1024 / 1024:.1f} MB; the limit is "
                f"{self.max_bytes // 1024 // 1024} MB. Export a web-sized copy "
                f"and upload that."
            )

        # Pillow is already a hard dependency (ImageField needs it). Reading the
        # dimensions must never be what breaks a save, so a file we cannot parse
        # is reported as "not an image" rather than raising out of here.
        try:
            from PIL import Image

            value.open()
            with Image.open(value) as im:
                fmt = (im.format or "").upper()
                width, height = im.size
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("That file could not be read as an image.")
        finally:
            # Leave the file where the rest of the save path expects it.
            try:
                value.seek(0)
            except Exception:
                pass

        if fmt not in ALLOWED_FORMATS:
            allowed = ", ".join(_FRIENDLY[f] for f in sorted(ALLOWED_FORMATS))
            errors.append(
                f"{fmt or 'That format'} is not accepted. Use {allowed}."
            )

        if max(width, height) > self.max_dimension:
            errors.append(
                f"Image is {width}x{height}px; the longest side must be "
                f"{self.max_dimension}px or less. Resize it and upload again."
            )

        if errors:
            raise ValidationError(errors)

    def __eq__(self, other):
        return (
            isinstance(other, CmsImageValidator)
            and self.max_dimension == other.max_dimension
            and self.max_bytes == other.max_bytes
        )


validate_cms_image = CmsImageValidator()
