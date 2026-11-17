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
import re
import unicodedata

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import live_rules
from .models import GroupSession, SessionFile, PrivateSession, PrivateSessionFile
from .views import _is_private_session_participant


# ──────────────────────────────────────────────────────────────────────────
# Policy helpers — the single source of truth for "may this user read this
# room file". config.media_security._check_session_file imports these rather
# than restating the rule, the same way it already imported _in_room: the
# bytes and the JSON listing must never disagree about who may see a file.
#
# RC-031 (docs/architecture/shiksha-remote/tasks/phase-5/RC-031.md) added the
# expiry clause. Before it, `expires_at` was write-only — set here at upload
# and read by nothing but purge_expired_session_files, which is registered in
# no celery-beat or cron entry anywhere in the repo. So "files self-destruct
# after live_file_retention_days" (this module's own docstring) was not true
# of any read path: an expired row was still listed, still carried a live
# URL, and that URL still served the bytes.
# ──────────────────────────────────────────────────────────────────────────


def file_is_expired(row, now=None):
    return row.expires_at is not None and row.expires_at <= (now or timezone.now())


def live_files(session):
    """The rows that still count as room files.

    Expiry is enforced at read time rather than trusted to the sweeper, so
    the promise holds whether or not purge_expired_session_files has run —
    which today it never does automatically. Expired rows also stop counting
    toward live_max_files_per_session: they are gone as far as every read
    path is concerned, so letting them keep occupying the quota would wedge
    uploads in any room older than the retention window.
    """
    return session.files.filter(expires_at__gt=timezone.now())


def can_read_group_file(user, row):
    """Who may read a SessionFile's bytes — session_files()'s own gate, plus
    an expiry clause that gate never had.

    Staff keep their existing review access to *group room* files (unchanged
    from the original rule), but expiry binds them too: retention is a
    promise to the people in the room, not an access-control convenience, so
    there is no role that can still fetch a file the product told a user had
    self-destructed.
    """
    if not user.is_authenticated:
        return False
    if file_is_expired(row):
        return False
    if user.is_staff:
        return True
    return _in_room(user, row.session) or user.id == row.session.host_id


def can_read_private_file(user, row):
    """Who may read a PrivateSessionFile's bytes.

    🔴 Deliberately NO staff bypass — this is a behaviour change, see
    RC-031's report. A 1:1 session's files are recipient-only; the previous
    rule let any is_staff account read a private tutoring session's
    homework, which is exactly the "private-recipient access to staff" the
    task forbids. Staff moderation of *group rooms* is untouched above.
    """
    if not user.is_authenticated:
        return False
    if file_is_expired(row):
        return False
    return _is_private_session_participant(row.session, user)


_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")
_CONTENT_TYPE_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def _safe_original_name(raw):
    """A display/download filename that cannot carry a path or a control
    character into a Content-Disposition header or a client's save dialog.

    This is the name echoed back as `name` and used by the browser when
    saving; the *stored* path is Django's, not this. Strips directory
    components (a multipart part may legally declare "../../x"), NFC-
    normalises, removes control characters and separators, and never returns
    empty.
    """
    name = unicodedata.normalize("NFC", raw or "").strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("", name).strip(" .")
    return (name or "file")[:255]


def _safe_content_type(raw):
    """The client-declared multipart content type, kept only when it is a
    well-formed type/subtype token.

    It is stored and echoed as a hint and is NOT what the download path
    serves — config/media_views.py derives the response Content-Type from
    the stored filename and refuses to serve anything scriptable inline.
    Sanitised anyway so an attacker-chosen string can never reach a header
    or a template through some later consumer.
    """
    value = (raw or "").split(";")[0].strip().lower()[:120]
    return value if _CONTENT_TYPE_RE.match(value) else ""


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
        rows = live_files(session).select_related("uploaded_by")
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

    # The count and the insert have to be one atomic unit: as a plain
    # read-then-write, N concurrent uploads all read count == limit-1, all
    # pass, and all insert, so the cap is exceeded by the concurrency level.
    # Locking the parent session row serialises uploads per room.
    # 🔴 On SQLite select_for_update() is a silent no-op, so the test suite
    # cannot prove this lock — deleting it would fail nothing locally. It is
    # load-bearing only on the real PG 16. See RC-031's report.
    with transaction.atomic():
        GroupSession.objects.select_for_update().filter(pk=session.pk).first()
        if live_files(session).count() >= s.live_max_files_per_session:
            return Response(
                {"detail": f"Max {s.live_max_files_per_session} files.", "code": "too_many"},
                status=409,
            )

        row = SessionFile.objects.create(
            session=session,
            uploaded_by=request.user,
            file=upload,
            original_name=_safe_original_name(upload.name),
            content_type=_safe_content_type(getattr(upload, "content_type", "")),
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


# ──────────────────────────────────────────────────────────────────────────
# PrivateSession files — same shape as the GroupSession views above, gated
# by _is_private_session_participant (teacher, requester, or an accepted
# group-type participant) instead of the join-time GroupSessionParticipant
# row, and broadcasting on private_session_chat_<id> (PrivateSessionChatConsumer)
# instead of group_session_chat_<id>.
# ──────────────────────────────────────────────────────────────────────────


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser])
def private_session_files(request, session_id):
    session = get_object_or_404(PrivateSession, id=session_id)
    if not _is_private_session_participant(session, request.user):
        return Response({"detail": "Not a participant."}, status=403)

    if request.method == "GET":
        rows = live_files(session).select_related("uploaded_by")
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

    # Same atomic quota as the group view above — see its comment for why
    # the count and the insert cannot be separate statements, and for the
    # SQLite caveat on select_for_update().
    with transaction.atomic():
        PrivateSession.objects.select_for_update().filter(pk=session.pk).first()
        if live_files(session).count() >= s.live_max_files_per_session:
            return Response(
                {"detail": f"Max {s.live_max_files_per_session} files.", "code": "too_many"},
                status=409,
            )

        row = PrivateSessionFile.objects.create(
            session=session,
            uploaded_by=request.user,
            file=upload,
            original_name=_safe_original_name(upload.name),
            content_type=_safe_content_type(getattr(upload, "content_type", "")),
            size_bytes=upload.size,
            expires_at=live_rules.file_expires_at(session),
        )
    data = _serialize(row, request)

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"private_session_chat_{session.id}",
                {"type": "session_file_added", "file": data},
            )
        except Exception:
            pass
    return Response(data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_private_session_file(request, session_id, file_id):
    session = get_object_or_404(PrivateSession, id=session_id)
    row = get_object_or_404(PrivateSessionFile, id=file_id, session=session)
    if request.user.id not in (row.uploaded_by_id, session.teacher_id):
        return Response({"detail": "Not allowed."}, status=403)

    row.file.delete(save=False)
    row.delete()

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"private_session_chat_{session.id}",
                {"type": "session_file_removed", "file_id": file_id},
            )
        except Exception:
            pass
    return Response(status=status.HTTP_204_NO_CONTENT)
