"""Session file sharing (design screen 05).

Everyone currently in the room may upload; the uploader or the host may
delete; files self-destruct after GlobalSettings.live_file_retention_days
(see live_rules.file_expires_at) unless a learner explicitly saved them to
their course.

Adapted from the design handoff's reference implementation
(``design_handoff_live_sessions/backend/sessions_app/live_files_views.py``).
The handoff's ``_in_room`` checks ``session.participants.filter(user=user,
left_at__isnull=True)`` — that relation didn't exist in this repo, so
``GroupSessionParticipant`` (sessions_app/models.py) was added as part of
this same change to back it. See that model's docstring for exactly what
"in room" can and can't promise today (best-effort, join-time signal; no
live disconnect wiring yet).
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import live_rules
from .models import GroupSession, SessionFile


def _serialize(f, request):
    return {
        "id": f.id,
        "name": f.original_name,
        "url": request.build_absolute_uri(f.file.url),
        "size_bytes": f.size_bytes,
        "content_type": f.content_type,
        "uploaded_by": f.uploaded_by.get_full_name() or f.uploaded_by.username,
        "uploaded_by_id": f.uploaded_by_id,
        "created_at": f.created_at,
        "expires_at": f.expires_at,
    }


def _in_room(user, session):
    return session.participants.filter(user=user, left_at__isnull=True).exists()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser])
def session_files(request, session_id):
    session = get_object_or_404(GroupSession, id=session_id)
    if not (_in_room(request.user, session) or request.user.id == session.host_id):
        return Response({"detail": "Join the room first."}, status=403)

    if request.method == "GET":
        rows = session.files.select_related("uploaded_by")
        return Response([_serialize(f, request) for f in rows])

    s = live_rules.rules()
    upload = request.FILES.get("file")
    if not upload:
        return Response({"detail": "No file."}, status=400)
    if upload.size > s.live_max_upload_mb * 1024 * 1024:
        return Response(
            {"detail": f"Max {s.live_max_upload_mb} MB.", "code": "too_large"},
            status=413,
        )
    if session.files.count() >= s.live_max_files_per_session:
        return Response(
            {"detail": f"Max {s.live_max_files_per_session} files.", "code": "too_many"},
            status=409,
        )

    row = SessionFile.objects.create(
        session=session,
        uploaded_by=request.user,
        file=upload,
        original_name=upload.name[:255],
        content_type=getattr(upload, "content_type", "")[:120],
        size_bytes=upload.size,
        expires_at=live_rules.file_expires_at(session),
    )
    data = _serialize(row, request)

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"group_session_chat_{session.id}",
                {"type": "session_file_added", "file": data},
            )
        except Exception:
            pass
    return Response(data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_session_file(request, session_id, file_id):
    session = get_object_or_404(GroupSession, id=session_id)
    row = get_object_or_404(SessionFile, id=file_id, session=session)
    if request.user.id not in (row.uploaded_by_id, session.host_id):
        return Response({"detail": "Not allowed."}, status=403)

    row.file.delete(save=False)
    row.delete()

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"group_session_chat_{session.id}",
                {"type": "session_file_removed", "file_id": file_id},
            )
        except Exception:
            pass
    return Response(status=status.HTTP_204_NO_CONTENT)
