from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
import mimetypes
import os

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from .media_security import is_authorized


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

    content_type, _ = mimetypes.guess_type(name)

    if getattr(settings, "MEDIA_SERVED_BY_NGINX", True):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/internal-media/{name}"
        return response

    return FileResponse(open(abs_path, "rb"), content_type=content_type)
