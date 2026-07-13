# PLACEMENT: backend/backend/chat/attachments.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/attachments.py
"""
chat/attachments.py — validation + classification for CC-012 (Communication
Center closure, Stage C).

Deliberately separate from materials/validators.py even though the shape is
similar (extension allowlist + size ceiling): a chat attachment is sent
between two people in real time and has a much smaller reasonable ceiling
than a teacher's study-material upload, and its extension policy is
narrower (no audio — voice notes are explicitly future-scope per the gap
analysis; no .odt/.ods/.odp/.rtf — kept to the formats CC-010 actually
names: Image / PDF / Document).

classify_and_validate() is the one entry point services.post_attachment_checked()
calls. It returns (meta_dict, None) on success or (None, error_message) on
failure — the same "(value, error)" shape post_message_checked() already
uses everywhere else in this app, so callers handle both the same way.
"""
import os

from django.conf import settings
from django.core.exceptions import ValidationError

# kind -> (content_type sniffed from extension, set of extensions)
# Deliberately narrow — CC-010's named message types are Image / PDF /
# Document. Spreadsheets and slides count as "Document" for this v1; nothing
# stops splitting them into their own kind later without a migration (kind
# is just a CharField).
_IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp"}
_PDF_EXT = {".pdf": "application/pdf"}
_DOCUMENT_EXT = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".csv": "text/csv",
}

_KIND_FOR_EXT = {}
for _ext in _IMAGE_EXT:
    _KIND_FOR_EXT[_ext] = "IMAGE"
for _ext in _PDF_EXT:
    _KIND_FOR_EXT[_ext] = "PDF"
for _ext in _DOCUMENT_EXT:
    _KIND_FOR_EXT[_ext] = "DOCUMENT"

_CONTENT_TYPE_FOR_EXT = {**_IMAGE_EXT, **_PDF_EXT, **_DOCUMENT_EXT}

ALLOWED_EXTENSIONS = frozenset(_KIND_FOR_EXT)

MAX_ATTACHMENT_MB = getattr(settings, "CHAT_MAX_ATTACHMENT_MB", 15)
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

UNSUPPORTED_TYPE_MSG = (
    "That file type isn't supported here. Allowed: images (JPG/PNG/GIF/"
    "WEBP), PDF, Word, PowerPoint, Excel, CSV, or plain text."
)
TOO_LARGE_MSG = "File is {size_mb} MB. Maximum allowed is {max_mb} MB."
EMPTY_FILE_MSG = "File appears to be empty."


def classify_and_validate(django_file):
    """Validate an uploaded chat attachment.

    Returns (meta, None) where meta = {"kind", "content_type", "name",
    "size"}, or (None, error_message) if the file is rejected. Never raises
    — callers (services.post_attachment_checked) turn a rejection into the
    same {"category": "attachment", "reason": ...} shape moderation/policy
    rejections already use, so the frontend's existing error-toast handling
    needs no special case for attachments.
    """
    name = getattr(django_file, "name", "") or "file"
    ext = os.path.splitext(name)[1].lower()

    kind = _KIND_FOR_EXT.get(ext)
    if kind is None:
        return None, UNSUPPORTED_TYPE_MSG

    size = getattr(django_file, "size", 0) or 0
    if size <= 0:
        return None, EMPTY_FILE_MSG
    if size > MAX_ATTACHMENT_BYTES:
        return None, TOO_LARGE_MSG.format(
            size_mb=round(size / (1024 * 1024), 1), max_mb=MAX_ATTACHMENT_MB,
        )

    return {
        "kind": kind,
        "content_type": _CONTENT_TYPE_FOR_EXT.get(ext, "application/octet-stream"),
        "name": name,
        "size": size,
    }, None


def validate_chat_attachment(django_file):
    """Django model-field validator (defense in depth alongside the explicit
    classify_and_validate() call in the view — mirrors materials/validators.py's
    validate_material_file so both upload paths fail the same way if a file
    ever reaches .save() without going through the view's own check, e.g.
    from the Django admin)."""
    meta, error = classify_and_validate(django_file)
    if error:
        raise ValidationError(error)
