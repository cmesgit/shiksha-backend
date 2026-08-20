"""config/upload_validation.py — shared server-side file-type validation.

Several upload endpoints (Explore documents, forum attachments, the
scholarship manual KYC document) accepted ANY file with no server-side type
check at all — only a client-side `accept=` attribute, which is trivially
bypassable. Since /media/ files are served back with a real
`Content-Type` guessed from the extension (see config/media_views.py) and
auth cookies are scoped to the whole `.shikshacom.com` apex domain, an
uploaded `.html`/`.svg` is stored XSS the moment anyone (a moderator, a
classmate, an admin reviewing a KYC document) opens its URL.

This does two independent checks, both required:
  1. Extension against an explicit allowlist (never a blocklist — new
     dangerous extensions get invented; a known-safe list doesn't).
  2. Magic-byte sniff against what that extension actually looks like, for
     every type where a signature exists (image/PDF/Office/zip formats).
     `.txt`/`.csv` have no reliable signature — an extension match is all
     that's possible for them, which is fine: text/csv can't execute as
     markup the way html/svg can, so it isn't the same class of risk.

No new dependency: Pillow (already installed for ProcessedImageField) does
the real decode for images; everything else is a handful of well-known
byte signatures, which is all that distinguishes "genuinely a PDF" from
"a .pdf-named HTML payload" without pulling in libmagic.
"""
import os

from django.core.exceptions import ValidationError

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}

# extension -> byte signatures it must start with. None = no reliable
# signature exists; extension-allowlist is the only check for that type.
_SIGNATURES = {
    ".pdf":  [b"%PDF-"],
    ".png":  [b"\x89PNG\r\n\x1a\n"],
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".gif":  [b"GIF87a", b"GIF89a"],
    ".webp": [b"RIFF"],
    ".doc":  [b"\xd0\xcf\x11\xe0"],   # legacy OLE compound format
    ".xls":  [b"\xd0\xcf\x11\xe0"],
    ".ppt":  [b"\xd0\xcf\x11\xe0"],
    ".docx": [b"PK\x03\x04"],         # modern Office = a zip container
    ".xlsx": [b"PK\x03\x04"],
    ".pptx": [b"PK\x03\x04"],
    ".zip":  [b"PK\x03\x04"],
    ".txt":  None,
    ".csv":  None,
}


def validate_upload(f, allowed_exts, *, max_mb=None):
    """Raises django.core.exceptions.ValidationError if `f` (a Django
    UploadedFile) doesn't belong to `allowed_exts`, is oversized, or its
    content doesn't match what its extension claims. Returns the (lowercase)
    extension on success. Leaves the file's read position at 0."""
    name = getattr(f, "name", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed_exts:
        allowed = ", ".join(sorted(allowed_exts))
        raise ValidationError(f"'{ext or 'unknown'}' isn't an allowed file type. Allowed: {allowed}.")

    if max_mb is not None and f.size > max_mb * 1024 * 1024:
        raise ValidationError(f"File is larger than the {max_mb} MB limit.")

    sigs = _SIGNATURES.get(ext)
    if sigs:
        head = f.read(max(len(s) for s in sigs))
        f.seek(0)
        if not any(head.startswith(sig) for sig in sigs):
            raise ValidationError("This file's content doesn't match its extension.")

    return ext
