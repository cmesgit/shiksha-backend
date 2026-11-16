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

# ── Explore library sets (shiksha-backend/docs/EXPLORE_VERIFICATION.md §7) ───────────
# The redesign's upload accepts "documents, slides, spreadsheets and data,
# images, drawings, code, audio, video and ZIP archives".
#
# The TYPE list below is the design's. The SIZE limit is not — see
# LIBRARY_MAX_MB at the end of this block.
#
# ⚠ This is a SEPARATE constant, not a widening of DOCUMENT_EXTS. That set is
# shared with forum attachments and the scholarship KYC upload, and neither of
# those should start accepting video and archives because the library does.
#
# ⚠ Every extension here needs a _SIGNATURES entry or a deliberate None.
# validate_upload() only sniffs when a signature exists, so an extension added
# to this list and forgotten below is allowed through on its name alone —
# which is precisely the hole this module was written to close. The test suite
# asserts the two stay in step.
#
# Still excluded, deliberately: .html, .svg, .xml and anything else a browser
# will execute as markup. /media/ is served back with a Content-Type guessed
# from the extension and auth cookies are scoped to the apex domain, so one of
# those is stored XSS the moment a moderator opens it. "Code" below means
# source text that is downloaded, never rendered.
LIBRARY_EXTS = DOCUMENT_EXTS | IMAGE_EXTS | {
    # data
    ".csv", ".tsv", ".json", ".txt", ".md", ".rtf", ".odt", ".ods", ".odp",
    # drawings
    ".dwg", ".dxf", ".psd", ".ai",
    # code — downloaded, never rendered by the browser
    ".py", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".sql", ".r", ".m",
    ".ipynb", ".yml", ".yaml",
    # audio and video
    ".mp3", ".wav", ".m4a", ".ogg", ".mp4", ".mov", ".webm", ".mkv",
    # archives
    ".zip", ".7z", ".tar", ".gz",
}

# The library's own ceiling. Deliberately not a change to any other caller's
# limit — validate_upload takes max_mb per call for exactly this reason.
#
# ⚠ 50, NOT the design's 200. Reverted by the owner on 2026-11-13. The file
# TYPES stay widened; only the size came back down. The reason is storage, not
# validation: dev and prod share one public Bunny zone which is served
# unauthenticated and has no backup, so a 200 MB ceiling on user-uploaded
# video was the part of this change that could not be undone after the fact.
# Raising it again is one line here plus MAX_MB in UploadPage.jsx — they must
# move together or the picker and the server disagree.
LIBRARY_MAX_MB = 50

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
    ".odt":  [b"PK\x03\x04"],       # OpenDocument is also a zip container
    ".ods":  [b"PK\x03\x04"],
    ".odp":  [b"PK\x03\x04"],
    ".7z":   [b"7z\xbc\xaf\x27\x1c"],
    ".gz":   [b"\x1f\x8b"],
    ".psd":  [b"8BPS"],
    ".mp3":  [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    ".wav":  [b"RIFF"],
    ".ogg":  [b"OggS"],
    ".mkv":  [b"\x1a\x45\xdf\xa3"],
    ".rtf":  [b"{\\rtf"],
    # ⚠ mp4/mov/m4a/webm have NO usable prefix signature here. The ISO-BMFF
    # family puts its "ftyp" box at offset 4, not 0, and WebM shares Matroska's
    # EBML header, so a startswith() check is the wrong shape for all four.
    # Listed as None rather than omitted so the "every LIBRARY_EXT is
    # accounted for" test passes deliberately rather than by oversight — see
    # _OFFSET_SIGNATURES below, which does check the ISO-BMFF ones properly.
    ".mp4":  None,
    ".mov":  None,
    ".m4a":  None,
    ".webm": None,
    # No reliable signature exists for plain text in any of its costumes.
    # That is acceptable for the same reason .txt always was: text cannot
    # execute as markup the way html/svg can. The allowlist is the control.
    ".txt":  None,
    ".csv":  None,
    ".tsv":  None,
    ".json": None,
    ".md":   None,
    ".dwg":  None,
    ".dxf":  None,
    ".ai":   None,
    ".py":   None, ".c": None, ".cpp": None, ".h": None, ".java": None,
    ".js":   None, ".ts": None, ".sql": None, ".r": None, ".m": None,
    ".ipynb": None, ".yml": None, ".yaml": None,
    ".tar":  None,
}

# Signatures that do not live at byte 0. ISO base-media files (mp4/mov/m4a)
# carry a 4-byte box length first, then "ftyp".
_OFFSET_SIGNATURES = {
    ".mp4": (4, [b"ftyp"]),
    ".mov": (4, [b"ftyp"]),
    ".m4a": (4, [b"ftyp"]),
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

    offset_rule = _OFFSET_SIGNATURES.get(ext)
    if offset_rule:
        offset, expected = offset_rule
        head = f.read(offset + max(len(s) for s in expected))
        f.seek(0)
        if not any(head[offset:].startswith(sig) for sig in expected):
            raise ValidationError("This file's content doesn't match its extension.")

    return ext
