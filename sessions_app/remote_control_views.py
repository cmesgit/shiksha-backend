"""Teacher -> student screen control (design screen 06).

Three rules, enforced here and not merely in the UI:
  1. the controller must be a teacher,
  2. the target must be publishing a screen-share track,
  3. one active grant per session.

Students can never request control of anyone's screen. The admin can disable
the whole feature with GlobalSettings.live_remote_access_enabled.

Adapted from the design handoff's reference implementation
(``design_handoff_live_sessions/backend/sessions_app/remote_control_views.py``)
to this repo's real role check: there is no scalar ``user.role`` attribute —
the real check is ``user.has_role("TEACHER")`` (RBAC, see accounts/models.py
and sessions_app/permissions.py's ``IsTeacher``). ``participant.is_sharing_screen``
is backed by the new ``GroupSessionParticipant`` model (see sessions_app/
models.py's module note next to it) — nothing currently flips that flag to
True (no LiveKit track-published webhook wired for group sessions yet), so
in practice ``request_remote_control`` will always 409 with "not_sharing"
until that producer exists. Left as-is rather than silently bypassing the
check, per the handoff's own "Open question" section.

See that same "Open question" section for why this only covers
authorisation + audit, not the actual input-replay transport.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import live_rules
from .models import GroupSession, RemoteControlGrant


def _push(session_id, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"group_session_chat_{session_id}", payload
        )
    except Exception:
        pass


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_remote_control(request, session_id):
    session = get_object_or_404(GroupSession, id=session_id)
    if not live_rules.features()["remote_access"]:
        return Response({"detail": "Disabled by the admin."}, status=403)
    if not request.user.has_role("TEACHER"):
        return Response(
            {"detail": "Only teachers may control a screen.", "code": "role"},
            status=403,
        )

    target_id = request.data.get("target_user_id")
    participant = session.participants.filter(
        user_id=target_id, left_at__isnull=True
    ).first()
    if not participant:
        return Response({"detail": "Not in the room."}, status=404)
    if not participant.is_sharing_screen:
        return Response(
            {"detail": "They must share a screen first.", "code": "not_sharing"},
            status=409,
        )
    if session.remote_grants.filter(status=RemoteControlGrant.STATUS_ACTIVE).exists():
        return Response(
            {"detail": "Another control session is active.", "code": "busy"},
            status=409,
        )

    grant = RemoteControlGrant.objects.create(
        session=session, controller=request.user, target_id=target_id
    )
    _push(
        session.id,
        {
            "type": "remote_control_requested",
            "grant_id": grant.id,
            "target_user_id": int(target_id),
            "controller": request.user.get_full_name() or request.user.username,
        },
    )
    return Response({"grant_id": grant.id, "status": grant.status}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def respond_remote_control(request, session_id):
    grant = get_object_or_404(
        RemoteControlGrant,
        id=request.data.get("grant_id"),
        session_id=session_id,
        target=request.user,  # only the target may answer
        status=RemoteControlGrant.STATUS_REQUESTED,
    )
    if request.data.get("allow"):
        grant.status = RemoteControlGrant.STATUS_ACTIVE
        grant.granted_at = timezone.now()
        grant.save(update_fields=["status", "granted_at"])
        _push(
            session_id,
            {
                "type": "remote_control_granted",
                "grant_id": grant.id,
                "controller_user_id": grant.controller_id,
                "target_user_id": grant.target_id,
            },
        )
    else:
        grant.status = RemoteControlGrant.STATUS_DECLINED
        grant.ended_at = timezone.now()
        grant.ended_by = "target"
        grant.save(update_fields=["status", "ended_at", "ended_by"])
        _push(session_id, {"type": "remote_control_declined", "grant_id": grant.id})
    return Response({"status": grant.status})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def revoke_remote_control(request, session_id):
    grant = get_object_or_404(
        RemoteControlGrant,
        id=request.data.get("grant_id"),
        session_id=session_id,
        status=RemoteControlGrant.STATUS_ACTIVE,
    )
    if request.user.id not in (
        grant.controller_id,
        grant.target_id,
        grant.session.host_id,
    ):
        return Response({"detail": "Not allowed."}, status=403)

    grant.status = RemoteControlGrant.STATUS_REVOKED
    grant.ended_at = timezone.now()
    grant.ended_by = "target" if request.user.id == grant.target_id else "controller"
    grant.save(update_fields=["status", "ended_at", "ended_by"])
    _push(session_id, {"type": "remote_control_revoked", "grant_id": grant.id})
    return Response({"status": grant.status})
