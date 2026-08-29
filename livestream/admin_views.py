"""Admin-facing livestream endpoints (is_staff-gated).

Backs the LMS Admin Console screens: Live Streams hub, Livestream Monitor,
Recordings library, and the Overview "Live now" feed. Read-mostly, plus admin
chat-post and force-end. Also hosts the authenticated client health-telemetry
ingest that feeds the Monitor's stream-health panel.
"""
import json
import logging

import redis
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status as http_status
from django.shortcuts import get_object_or_404

from accounts.permissions import IsAdmin
from .models import (
    LiveSession,
    LiveSessionChatMessage,
    LiveSessionViewerSample,
    LiveKitWebhookEvent,
    StreamHealthSample,
)
from .services import attendance as attendance_svc
from .services.room_admin import close_room
from .views import broadcast_session_update

logger = logging.getLogger(__name__)
_r = redis.Redis(host="127.0.0.1", port=6379, db=0)

LIVE_STATES = [
    LiveSession.STATUS_LIVE,
    LiveSession.STATUS_WAITING,
    LiveSession.STATUS_PAUSED,
    LiveSession.STATUS_RECONNECTING,
]


def _stream_row(s):
    return {
        "id": str(s.id),
        "title": s.title,
        "status": s.computed_status(),
        "course_name": s.course.title if s.course_id else "",
        "subject_name": s.subject.name if s.subject_id else "",
        "batch_code": (s.batch.code if s.batch_id and s.batch else None),
        "batch_name": (s.batch.name if s.batch_id and s.batch else None),
        "teacher": s.created_by.email if s.created_by_id else "",
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "actual_started_at": s.actual_started_at.isoformat() if s.actual_started_at else None,
        "actual_ended_at": s.actual_ended_at.isoformat() if s.actual_ended_at else None,
        "peak_viewers": s.peak_viewers,
        "watching": attendance_svc.current_watching(s),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_stream_list(request):
    """GET /livestream/admin/streams/?status=live|scheduled|all"""
    status_q = (request.query_params.get("status") or "all").lower()
    now = timezone.now()
    qs = LiveSession.objects.select_related("course", "subject", "batch", "created_by")

    if status_q == "live":
        qs = qs.filter(status__in=LIVE_STATES, end_time__gte=now)
    elif status_q == "scheduled":
        qs = qs.filter(status=LiveSession.STATUS_SCHEDULED, start_time__gte=now)
    else:
        # Default: everything currently relevant (live + upcoming + recent).
        from datetime import timedelta
        qs = qs.filter(end_time__gte=now - timedelta(hours=6))

    qs = qs.order_by("-status", "start_time")[:200]
    return Response({"data": [_stream_row(s) for s in qs]})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_live_now(request):
    """GET /livestream/admin/live-now/ — Overview 'Live now' feed."""
    now = timezone.now()
    qs = (
        LiveSession.objects.select_related("course", "subject", "batch", "created_by")
        .filter(status__in=[LiveSession.STATUS_LIVE, LiveSession.STATUS_RECONNECTING], end_time__gte=now)
        .order_by("start_time")[:50]
    )
    return Response({"data": [_stream_row(s) for s in qs]})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_stream_detail(request, session_id):
    """GET /livestream/admin/streams/<id>/ — full monitor payload."""
    s = get_object_or_404(
        LiveSession.objects.select_related("course", "subject", "batch", "created_by"),
        id=session_id,
    )

    intervals_by_user = {}
    for iv in s.attendance_intervals.all():
        intervals_by_user.setdefault(iv.user_id, []).append(iv)

    attendance = [
        {
            "user_name": (a.user.get_full_name() if a.user_id and hasattr(a.user, "get_full_name") else ""),
            "user_email": a.user.email if a.user_id else "",
            "joined_at": a.joined_at.isoformat() if a.joined_at else None,
            "left_at": a.left_at.isoformat() if a.left_at else None,
            "total_seconds": a.total_seconds,
            "online": a.left_at is None and a.joined_at is not None,
            "rejoin_count": max(len(intervals_by_user.get(a.user_id, [])) - 1, 0),
            "reconciled": any(iv.closed_by_reconcile for iv in intervals_by_user.get(a.user_id, [])),
        }
        for a in s.attendances.select_related("user").all()
    ]

    chat = [
        {
            "sender": m.sender_name,
            "text": m.text,
            "isTeacher": m.is_teacher,
            "time": m.created_at.isoformat(),
        }
        for m in s.chat_messages.all().order_by("created_at")[:200]
    ]

    presenter_samples = list(s.health_samples.filter(is_presenter=True).order_by("-ts")[:200])
    health_qs_samples = presenter_samples or list(s.health_samples.order_by("-ts")[:200])
    latest_health = health_qs_samples[0] if health_qs_samples else None
    health = None
    if latest_health:
        health = {
            "bitrate_kbps": latest_health.bitrate_kbps,
            "fps": latest_health.fps,
            "latency_ms": latest_health.latency_ms,
            "packet_loss": latest_health.packet_loss,
            "quality": latest_health.quality,
            "ts": latest_health.ts.isoformat(),
        }
    # Windowed trend, oldest→newest, for the Monitor's health-over-time chart.
    health_samples = [
        {
            "ts": h.ts.isoformat(),
            "bitrate_kbps": h.bitrate_kbps,
            "fps": h.fps,
            "latency_ms": h.latency_ms,
            "packet_loss": h.packet_loss,
            "quality": h.quality,
        }
        for h in reversed(health_qs_samples)
    ]

    viewer_samples = [
        {"ts": v.ts.isoformat(), "viewers": v.viewers}
        for v in s.viewer_samples.order_by("ts")[:500]
    ]

    return Response({
        "stream": _stream_row(s),
        "attendance": attendance,
        "chat": chat,
        "health": health,
        "health_samples": health_samples,
        "viewer_samples": viewer_samples,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_stream_chat(request, session_id):
    """POST /livestream/admin/streams/<id>/chat/ {text} — admin posts to the room."""
    s = get_object_or_404(LiveSession, id=session_id)
    text = (request.data.get("text") or "").strip()
    if not text:
        return Response({"detail": "text required"}, status=http_status.HTTP_400_BAD_REQUEST)

    sender_name = "Admin"
    msg = LiveSessionChatMessage.objects.create(
        session=s, user=request.user, sender_name=sender_name, text=text, is_teacher=False,
    )
    payload = {
        "sender": sender_name,
        "text": text,
        "role": "ADMIN",
        "isTeacher": False,
        "time": msg.created_at.isoformat(),
        "sender_id": str(request.user.id),
    }
    # Mirror the consumer: push to Redis fast-path + broadcast to the room.
    try:
        key = f"chat:{s.id}"
        _r.rpush(key, json.dumps(payload))
        _r.expire(key, 86400)
    except Exception:
        logger.warning("admin chat: redis push failed", exc_info=True)
    try:
        cl = get_channel_layer()
        if cl:
            async_to_sync(cl.group_send)(f"session_{s.id}", {"type": "chat_message", "data": payload})
    except Exception:
        logger.warning("admin chat: broadcast failed", exc_info=True)

    return Response(payload, status=http_status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_stream_end(request, session_id):
    """POST /livestream/admin/streams/<id>/end/ — admin force-ends a stream."""
    s = get_object_or_404(LiveSession, id=session_id)
    if s.status in (LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED):
        return Response({"detail": "Session already closed.", "status": s.status}, status=400)

    now = timezone.now()
    attendance_svc.close_all_open(s, when=now)
    s.status = LiveSession.STATUS_COMPLETED
    s.teacher_left_at = None
    if s.actual_ended_at is None:
        s.actual_ended_at = now
    s.save(update_fields=["status", "teacher_left_at", "actual_ended_at"])
    close_room(s.room_name)
    broadcast_session_update(s)
    return Response({"detail": "Session ended.", "status": "COMPLETED"})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_recordings(request):
    """GET /livestream/admin/recordings/?course=&batch=&q= — recordings library."""
    from courses.models_recordings import SessionRecording

    qs = SessionRecording.objects.select_related(
        "subject", "subject__course", "batch", "uploaded_by", "live_session"
    )
    course = request.query_params.get("course")
    batch = request.query_params.get("batch")
    q = request.query_params.get("q")
    if course:
        qs = qs.filter(subject__course_id=course)
    if batch:
        qs = qs.filter(batch_id=batch)
    if q:
        qs = qs.filter(title__icontains=q)

    data = [
        {
            "id": str(r.id),
            "title": r.title,
            # DESCRIPTION and the *_id fields below exist so the admin edit
            # form can be seeded from this row. This is a hand-built dict, not
            # SessionRecordingSerializer, and it returned display NAMES only —
            # so an edit modal had nothing to pre-select with and no way to
            # send a valid batch/chapter back.
            "description": r.description,
            "subject_id": str(r.subject_id) if r.subject_id else None,
            "subject_name": r.subject.name if r.subject_id else "",
            "course_id": (
                str(r.subject.course_id)
                if r.subject_id and r.subject.course_id else None
            ),
            "course_name": (r.subject.course.title if r.subject_id and r.subject.course_id else ""),
            "batch_id": str(r.batch_id) if r.batch_id else None,
            "batch_name": r.batch.name if r.batch_id and r.batch else None,
            "chapter_id": str(r.chapter_id) if r.chapter_id else None,
            "chapter_note": r.chapter_note,
            "no_specific_chapter": r.no_specific_chapter,
            "session_date": r.session_date.isoformat() if r.session_date else None,
            "duration_seconds": r.duration_seconds,
            "trim_start_seconds": r.trim_start_seconds,
            "trim_end_seconds": r.trim_end_seconds,
            "status": r.get_status_display(),
            # The raw int alongside the display string. A UI gating the trim
            # control on "is this finished" must not string-match "Finished" —
            # that breaks the moment the label is reworded or translated.
            "status_code": r.status,
            # The ONLY playback handle there is — SessionRecording has no
            # video_url/playback_url field; every client composes the Bunny
            # embed URL from library id + this. Omitting it (as this
            # hand-built dict used to) is why the admin Recordings page could
            # list recordings but never play one. The shared
            # SessionRecordingSerializer the student/teacher apps use has
            # always exposed it.
            "bunny_video_id": r.bunny_video_id,
            "thumbnail_url": r.thumbnail_url,
            "is_published": r.is_published,
            "live_session_id": str(r.live_session_id) if r.live_session_id else None,
            "uploaded_by": r.uploaded_by.email if r.uploaded_by_id else "",
            "created_at": r.created_at.isoformat(),
        }
        for r in qs.order_by("-created_at")[:300]
    ]
    return Response({"data": data})


# ─────────────────────────────────────────────────────────────────────────
# Client health telemetry ingest (authenticated participants, not admin-only).
# LiveKit doesn't push quality stats via webhook, so presenter/viewer clients
# POST periodic samples here — the durable capture path for stream health.
# ─────────────────────────────────────────────────────────────────────────
def _to_int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ingest_health(request, session_id):
    """POST /livestream/sessions/<id>/health/ {bitrate_kbps,fps,latency_ms,packet_loss,quality,is_presenter}"""
    s = get_object_or_404(LiveSession, id=session_id)
    is_presenter = bool(request.data.get("is_presenter")) or (
        str(s.created_by_id) == str(request.user.id)
    )
    StreamHealthSample.objects.create(
        session=s,
        user=request.user,
        bitrate_kbps=_to_int(request.data.get("bitrate_kbps")),
        fps=_to_int(request.data.get("fps")),
        latency_ms=_to_int(request.data.get("latency_ms")),
        packet_loss=_to_float(request.data.get("packet_loss")),
        quality=(request.data.get("quality") or "")[:20],
        is_presenter=is_presenter,
    )
    return Response({"ok": True}, status=http_status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────────
# Webhook audit trail — surfaces LiveKitWebhookEvent (the idempotent,
# write-before-dispatch log every inbound LiveKit webhook already lands in)
# so ops can diagnose processing failures instead of only ever seeing them
# in server logs.
# ─────────────────────────────────────────────────────────────────────────
def _webhook_event_row(w):
    return {
        "id": str(w.id),
        "event_id": w.event_id,
        "event_type": w.event_type,
        "room_name": w.room_name,
        "session_id": str(w.session_id) if w.session_id else None,
        "processed": w.processed,
        "error": w.error,
        "received_at": w.received_at.isoformat(),
        "payload": w.payload,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_webhook_events(request):
    """GET /livestream/admin/webhook-events/?status=failed|unprocessed|all&event_type=&room_name=&q="""
    status_q = (request.query_params.get("status") or "failed").lower()
    qs = LiveKitWebhookEvent.objects.all()

    if status_q == "failed":
        qs = qs.exclude(error="")
    elif status_q == "unprocessed":
        qs = qs.filter(processed=False)
    # else "all": no filter

    event_type = request.query_params.get("event_type")
    if event_type:
        qs = qs.filter(event_type=event_type)
    room_name = request.query_params.get("room_name")
    if room_name:
        qs = qs.filter(room_name__icontains=room_name)

    qs = qs.order_by("-received_at")[:300]

    all_events = LiveKitWebhookEvent.objects.all()
    counts = {
        "total": all_events.count(),
        "unprocessed": all_events.filter(processed=False).count(),
        "failed": all_events.exclude(error="").count(),
    }

    return Response({"data": [_webhook_event_row(w) for w in qs], "counts": counts})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_stream_spectate(request, session_id):
    """POST /livestream/admin/streams/<id>/spectate/ — watch a class live.

    Returns a SUBSCRIBE-ONLY token. Every other grant in this codebase can
    publish at least a microphone, so reusing one would have let an admin
    speak into a live class; this one sets can_publish=False and hidden=True.

    Hidden means the room is not told. That is the product decision, and it
    is why the spectate is logged: silent monitoring is one thing, untraceable
    monitoring is another. The row records who, which class, and when.
    """
    from .models import LiveSessionSpectate
    from .services.token import generate_livekit_token

    s = get_object_or_404(LiveSession, id=session_id)

    if s.status in (LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED):
        return Response({"detail": "That class is not running."}, status=400)
    if not s.room_name:
        return Response({"detail": "That class has no room yet."}, status=400)

    LiveSessionSpectate.objects.create(
        session=s,
        admin=request.user,
        admin_email=(request.user.email or request.user.username or "")[:254],
        reason=(request.data.get("reason") or "")[:255],
    )
    logger.info("admin spectate: %s -> session %s", request.user.email, s.id)

    token = generate_livekit_token(
        user=request.user, session=s, spectator=True,
        display_name="Observer",
    )
    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": s.room_name,
        "title": s.title,
        "subject_name": s.subject.name if s.subject_id else "",
        "batch_name": s.batch.name if s.batch_id else "",
    })
