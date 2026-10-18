"""Account-free visitors for instant collaboration only.

The signed credential proves ownership of one pending request. It is never a
site login and cannot be used on scheduled groups or other REST endpoints.
"""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from livekit.api import AccessToken, VideoGrants
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from .models import GroupSession, InstantCollaborationGuest

logger = logging.getLogger(__name__)
SALT = "instant-collaboration-guest-v1"


class GuestRequestThrottle(SimpleRateThrottle):
    scope = "instant_guest_request"
    rate = "60/hour"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class CodeLookupThrottle(GuestRequestThrottle):
    scope = "collaboration_code_lookup"
    rate = "120/hour"


def _credential(guest):
    return signing.dumps({"session": str(guest.session_id), "guest": str(guest.id)}, salt=SALT)


def _from_credential(session_id, credential):
    try:
        payload = signing.loads(credential, salt=SALT, max_age=4 * 60 * 60)
        if payload.get("session") != str(session_id):
            return None
        return InstantCollaborationGuest.objects.select_related("session").get(
            pk=payload["guest"], session_id=session_id
        )
    except (signing.BadSignature, KeyError, TypeError, ValueError, ValidationError,
            InstantCollaborationGuest.DoesNotExist):
        return None


def active_guest_count(session):
    return InstantCollaborationGuest.objects.filter(
        session=session, status="admitted",
        last_seen_at__gte=timezone.now() - timedelta(seconds=30),
    ).count()


def _room_open(session):
    if session.session_type != "instant" or session.status != "live" or not session.room_started_at:
        return False
    return timezone.now() < session.room_started_at + timedelta(minutes=session.duration_minutes)


def _guest_token(guest):
    session = guest.session
    token = AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    token.with_identity(f"guest_{guest.id}")
    token.with_name(guest.name)
    token.with_metadata(json.dumps({
        "role": "guest", "type": "instant_collaboration_guest",
        "guest_id": str(guest.id), "session_id": str(session.id),
    }))
    # LiveKit bearer tokens cannot be revoked. Keep the reconnect window short;
    # removal also disconnects the participant and blocks token renewal.
    token.with_ttl(timedelta(minutes=1))
    token.with_grants(VideoGrants(
        room_join=True, room=session.room_name,
        can_publish=True, can_subscribe=True, can_publish_data=False,
    ))
    return token.to_jwt()


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GuestRequestThrottle])
def request_guest_join(request):
    code = str(request.data.get("code") or "").strip().lower().rstrip("/").split("/")[-1]
    name = " ".join(str(request.data.get("name") or "").strip().split())
    if len(name) < 2 or len(name) > 80:
        return Response({"detail": "Enter a name between 2 and 80 characters."}, status=400)
    if not code or len(code) > 80:
        return Response({"detail": "Enter a valid collaboration code."}, status=400)
    session = GroupSession.objects.filter(short_code=code, session_type="instant").first()
    if session is None:
        # A host's copied room URL may contain the UUID instead of short code.
        try:
            session = GroupSession.objects.filter(pk=code, session_type="instant").first()
        except (ValueError, TypeError, ValidationError):
            session = None
    if session is None or not _room_open(session):
        return Response({"detail": "This collaboration is unavailable."}, status=404)
    if session.instant_guests.filter(status="pending").count() >= 100:
        return Response({"detail": "The waiting room is full."}, status=429)
    guest = InstantCollaborationGuest.objects.create(session=session, name=name)
    return Response({
        "session_id": str(session.id), "credential": _credential(guest),
        "status": "pending", "topic": session.topic,
        "ends_at": (session.room_started_at + timedelta(minutes=session.duration_minutes)).isoformat(),
    }, status=202)


@api_view(["POST"])
@permission_classes([AllowAny])
def guest_status(request, session_id):
    guest = _from_credential(session_id, request.data.get("credential"))
    if guest is None:
        return Response({"detail": "Guest pass is invalid or expired."}, status=403)
    if not _room_open(guest.session):
        return Response({"status": "ended"})
    if guest.status == "admitted":
        guest.last_seen_at = timezone.now()
        guest.save(update_fields=["last_seen_at"])
    return Response({
        "status": guest.status, "name": guest.name,
        "topic": guest.session.topic,
        "ends_at": (guest.session.room_started_at + timedelta(minutes=guest.session.duration_minutes)).isoformat(),
    })


@api_view(["POST"])
@permission_classes([AllowAny])
def guest_join(request, session_id):
    guest = _from_credential(session_id, request.data.get("credential"))
    if guest is None:
        return Response({"detail": "Guest pass is invalid or expired."}, status=403)
    if not _room_open(guest.session) or guest.status != "admitted":
        return Response({"detail": "Host approval is required."}, status=403)
    with transaction.atomic():
        session = GroupSession.objects.select_for_update().get(pk=session_id)
        if not _room_open(session):
            return Response({"detail": "Collaboration has ended."}, status=410)
        active_guests = active_guest_count(session)
        counted = guest.last_seen_at and guest.last_seen_at >= timezone.now() - timedelta(seconds=30)
        if (session.active_connections or 0) + active_guests >= 50 and not counted:
            return Response({"detail": "This collaboration is full."}, status=409)
        guest.last_seen_at = timezone.now()
        guest.save(update_fields=["last_seen_at"])
    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": _guest_token(guest), "room": guest.session.room_name,
        "name": guest.name,
        "ends_at": (guest.session.room_started_at + timedelta(minutes=guest.session.duration_minutes)).isoformat(),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_guest(request, session_id, guest_id):
    guest = InstantCollaborationGuest.objects.select_related("session").filter(
        pk=guest_id, session_id=session_id, session__session_type="instant"
    ).first()
    if guest is None:
        return Response({"detail": "Guest not found."}, status=404)
    if guest.session.host_id != request.user.id:
        return Response({"detail": "Only the host can remove a guest."}, status=403)
    guest.status = "removed"
    guest.resolved_at = timezone.now()
    guest.save(update_fields=["status", "resolved_at"])
    try:
        from livestream.services.room_admin import remove_participant
        remove_participant(guest.session.room_name, f"guest_{guest.id}")
    except Exception:
        # A pending guest was never in LiveKit; admitted guests are also
        # disconnected by their next status poll if the admin API is down.
        logger.warning("Could not disconnect collaboration guest %s", guest.id, exc_info=True)
    return Response({"status": "removed"})
