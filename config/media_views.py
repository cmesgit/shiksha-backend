from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
import mimetypes
import os
import posixpath
from urllib.parse import quote

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from .media_security import is_authorized


# Types we are willing to hand a browser to render IN PLACE. Everything else
# — including anything we fail to recognise — is forced to download as an
# opaque octet-stream instead.
#
# This is an allowlist rather than a blocklist of dangerous types because
# private media here is user-uploaded and served from the API's own origin:
# a type we forgot to block renders as same-origin content. The notable
# deliberate omission is image/svg+xml — SVG is an image everywhere else in
# this codebase, but it carries script and so is never safe to render inline
# from an upload path.
_INLINE_SAFE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/avif", "image/heic",
    "application/pdf",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/mp4",
    "text/plain",
})


def _disposition(name, content_type):
    """(response content type, Content-Disposition header value).

    The filename offered to the browser is the *stored* basename, not the
    uploader's original_name — this view is generic across every private
    media path and has no model to look that up in. Storage may have
    suffixed it for uniqueness, so it can differ slightly from what was
    uploaded; that is cosmetic. It is RFC 5987 percent-encoded either way,
    so a filename can never break out of the header.
    """
    inline = content_type in _INLINE_SAFE_TYPES
    filename = posixpath.basename(name) or "download"
    encoded = quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return (
        content_type if inline else "application/octet-stream",
        f"{kind}; filename*=UTF-8''{encoded}",
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def secure_media_view(request, name):
    """GET /api/media/secure/<name> — the only path a private media file's
    URL now points at (see SecureLocalStorage.url). Checks per-path
    authorization (config.media_security.is_authorized), then hands the
    actual bytes off to nginx via X-Accel-Redirect instead of streaming
    them through this Django worker — nginx's `internal-media` location is
    unreachable from outside, so this is the only door in.

    Falls back to streaming the file directly (FileResponse) when running
    without nginx in front (local dev via `manage.py runserver`) — nginx
    is the only thing that understands X-Accel-Redirect, and local dev has
    no nginx at all.
    """
    # Defends against a name that escapes MEDIA_ROOT via "../" — MEDIA_ROOT
    # is joined below either way, but reject early rather than rely solely
    # on nginx's alias resolution to contain it.
    abs_path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, name))
    if not abs_path.startswith(os.path.normpath(settings.MEDIA_ROOT) + os.sep):
        raise Http404()

    if not is_authorized(request, name):
        raise Http404()

    if not os.path.isfile(abs_path):
        raise Http404()

    guessed, _ = mimetypes.guess_type(name)
    content_type, disposition = _disposition(name, guessed)

    if getattr(settings, "MEDIA_SERVED_BY_NGINX", True):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/internal-media/{name}"
    else:
        response = FileResponse(open(abs_path, "rb"), content_type=content_type)

    response["Content-Disposition"] = disposition
    # Set explicitly rather than relying on SecurityMiddleware's
    # SECURE_CONTENT_TYPE_NOSNIFF: on the X-Accel-Redirect path nginx builds
    # the body-carrying response from this header set, and this is the one
    # response in the project where a wrong sniff means executing an
    # uploaded file as same-origin content.
    response["X-Content-Type-Options"] = "nosniff"
    return response
