from django.conf import settings
from .serializers import (
    LiveSessionCreateSerializer,
    LiveSessionListSerializer,
)
from .services.token import generate_livekit_token
from .models import LiveSession, LiveSessionAttendance
from enrollments.models import Enrollment
from livekit.api import WebhookReceiver
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from django.db.models import Q
from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from livestream.services.session_state import set_session_state


logger = logging.getLogger(__name__)


# =========================
# BROADCAST HELPERS
# =========================

def broadcast_session_update(session):
    """Broadcast status change to everyone inside the session room."""
    channel_layer = get_channel_layer()

    # Update Redis state cache (safe — never breaks if Redis is down)
    try:
        set_session_state(session)
    except Exception:
        pass

    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"session_{session.id}",
        {
            "type": "session_update",
            "data": {
                "status": session.computed_status(),
                "teacher_left_at": (
                    session.teacher_left_at.isoformat()
                    if session.teacher_left_at else None
                ),
            },
        },
    )

    # Also notify the session list page
    broadcast_course_sessions_update(session)


def broadcast_course_sessions_update(session):
    """Broadcast session changes to the session list page (LiveSessions.jsx)."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"course_sessions_{session.course_id}",
        {
            "type": "session_list_update",
            "data": {
                "id": str(session.id),
                "title": session.title,
                "start_time": session.start_time.isoformat(),
                "end_time": session.end_time.isoformat(),
                "status": session.status,
                "teacher_left_at": (
                    session.teacher_left_at.isoformat()
                    if session.teacher_left_at else None
                ),
                "subject_id": str(session.subject_id),
                "teacher": session.created_by.email if session.created_by else "",
            },
        }
    )


# =========================
# STUDENT SESSION LIST
# =========================

class StudentLiveSessionListView(generics.ListAPIView):
    serializer_class = LiveSessionListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        if not user.has_role("STUDENT"):
            raise PermissionDenied("Only students allowed.")

        course_id = self.request.query_params.get("course_id")
        subject_id = self.request.query_params.get("subject_id")

        active_courses = Enrollment.objects.filter(
            user=user,
            status=Enrollment.STATUS_ACTIVE
        ).values_list("course_id", flat=True)

        queryset = (
            LiveSession.objects
            .filter(course_id__in=active_courses)
            .select_related("course", "subject", "created_by")
        )

        if course_id:
            queryset = queryset.filter(course_id=course_id)

        if subject_id:
            queryset = queryset.filter(subject_id=subject_id)

        now = timezone.now()
        cutoff = now - timedelta(hours=24)
        queryset = queryset.filter(end_time__gte=cutoff)

        return queryset.order_by("start_time")


# =========================
# TEACHER SESSION LIST
# =========================

class TeacherLiveSessionListView(generics.ListAPIView):
    serializer_class = LiveSessionListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.request.query_params.get("subject_id")

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        now = timezone.now()
        cutoff = now - timedelta(hours=24)

        if subject_id:
            if not user.subject_assignments.filter(subject_id=subject_id).exists():
                raise PermissionDenied("Not assigned to this subject.")

            return (
                LiveSession.objects
                .filter(subject_id=subject_id)
                .filter(end_time__gte=cutoff)
                .select_related("course", "subject", "created_by")
                .order_by("start_time")
            )

        assigned_subject_ids = user.subject_assignments.values_list(
            "subject_id", flat=True)

        return (
            LiveSession.objects
            .filter(subject_id__in=assigned_subject_ids)
            .filter(end_time__gte=cutoff)
            .select_related("course", "subject", "created_by")
            .order_by("start_time")
        )


# =========================
# JOIN SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)
    now = timezone.now()

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session was cancelled."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session has ended."}, status=400)

    if now >= session.end_time:
        session.status = LiveSession.STATUS_COMPLETED
        session.save(update_fields=["status"])
        return Response({"detail": "Session has ended."}, status=400)

    if session.teacher_left_at:
        diff = now - session.teacher_left_at
        if diff > timedelta(minutes=60):
            session.status = LiveSession.STATUS_COMPLETED
            session.teacher_left_at = None
            session.save(update_fields=["status", "teacher_left_at"])
            return Response({"detail": "Session has ended."}, status=400)

    # ── Student ──
    if user.has_role("STUDENT"):
        is_enrolled = Enrollment.objects.filter(
            user=user,
            course=session.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled"}, status=403)

        if now < session.start_time - timedelta(minutes=15):
            return Response({"detail": "Too early"}, status=403)

        if session.teacher_left_at:
            if now - session.teacher_left_at > timedelta(minutes=60):
                return Response({"detail": "Session ended"}, status=403)

        is_teacher = False

    # ── Teacher ──
    elif user.has_role("TEACHER"):
        if not session.subject.subject_teachers.filter(teacher=user).exists():
            return Response({"detail": "Not assigned"}, status=403)

        is_creator = str(session.created_by_id) == str(user.id)
        is_teacher = is_creator

        # Revive session if teacher reconnects within 30 min
        if is_creator and session.teacher_left_at:
            if now <= session.teacher_left_at + timedelta(minutes=30):
                session.teacher_left_at = None
                session.status = LiveSession.STATUS_LIVE
                session.save(update_fields=["teacher_left_at", "status"])

    else:
        return Response({"detail": "Unauthorized"}, status=403)

    token = generate_livekit_token(
        user=user,
        session=session,
        is_teacher=is_teacher,
    )

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": "PRESENTER" if is_teacher else "STUDENT",
    })


# =========================
# CREATE SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_live_session(request):
    serializer = LiveSessionCreateSerializer(
        data=request.data,
        context={"request": request}
    )

    if serializer.is_valid():
        session = serializer.save()
        broadcast_course_sessions_update(session)
        return Response(
            {
                "id": session.id,
                "room": session.room_name,
                "status": session.status,
            },
            status=status.HTTP_201_CREATED
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# =========================
# CANCEL SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)

    if not user.has_role("TEACHER"):
        return Response({"detail": "Only teachers can cancel sessions."}, status=403)

    if session.created_by != user:
        return Response({"detail": "You can only cancel your own sessions."}, status=403)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session is already cancelled."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Cannot cancel a completed session."}, status=400)

    if timezone.now() >= session.start_time:
        return Response({"detail": "Cannot cancel a session that has already started. Use End instead."}, status=400)

    session.status = LiveSession.STATUS_CANCELLED
    session.save(update_fields=["status"])
    broadcast_course_sessions_update(session)

    return Response({"detail": "Session cancelled successfully."})


# =========================
# END SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def end_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)

    if not user.has_role("TEACHER"):
        return Response({"detail": "Only teachers can end sessions."}, status=403)

    if str(session.created_by_id) != str(user.id):
        return Response({"detail": "Only the session creator can end it."}, status=403)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session already completed."}, status=400)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session is cancelled."}, status=400)

    session.status = LiveSession.STATUS_COMPLETED
    session.teacher_left_at = None
    session.save(update_fields=["status", "teacher_left_at"])
    broadcast_session_update(session)
    return Response({"detail": "Session ended.", "status": "COMPLETED"})


# =========================
# PAUSE / RESUME SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def pause_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)

    if not user.has_role("TEACHER"):
        return Response({"detail": "Only teachers can pause sessions."}, status=403)

    if str(session.created_by_id) != str(user.id):
        return Response({"detail": "Only the session creator can pause."}, status=403)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Cannot pause a cancelled session."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Cannot pause a completed session."}, status=400)

    if session.status == LiveSession.STATUS_PAUSED and not session.teacher_left_at:
        # Resume
        session.status = LiveSession.STATUS_LIVE
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        broadcast_session_update(session)
        return Response({"detail": "Session resumed.", "status": "LIVE"})

    # Pause — don't set teacher_left_at so the reconnect timer doesn't start
    session.status = LiveSession.STATUS_PAUSED
    session.teacher_left_at = None
    session.save(update_fields=["status", "teacher_left_at"])
    broadcast_session_update(session)
    return Response({"detail": "Session paused.", "status": "PAUSED"})


# =========================
# LIVEKIT WEBHOOK
# =========================

@csrf_exempt
def livekit_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    receiver = WebhookReceiver(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    try:
        event = receiver.receive(
            request.body,
            request.headers.get("Authorization"),
        )

        logger.info(f"LiveKit event: {event.event}")

        handlers = {
            "participant_joined": _handle_participant_join,
            "participant_left": _handle_participant_left,
            "room_started": _handle_room_started,
            "room_finished": _handle_room_finished,
        }

        handler = handlers.get(event.event)
        if handler:
            handler(event)

        return HttpResponse(status=200)

    except Exception:
        logger.exception("Webhook error")
        return HttpResponse(status=400)


@transaction.atomic
def _handle_participant_join(event):
    session = LiveSession.objects.filter(room_name=event.room.name).first()
    if not session:
        return

    user_id = str(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    LiveSessionAttendance.objects.update_or_create(
        session=session,
        user=user,
        defaults={"joined_at": timezone.now()}
    )

    session.last_activity_at = timezone.now()

    if str(session.created_by_id) == user_id:
        session.teacher_left_at = None
        session.status = LiveSession.STATUS_LIVE

    session.save(update_fields=["teacher_left_at",
                 "status", "last_activity_at"])
    broadcast_session_update(session)

    # Notify enrolled students when teacher goes live
    if str(session.created_by_id) == user_id:
        from livestream.services.notifications import push_ws_notification
        students = Enrollment.objects.filter(
            course=session.course,
            status=Enrollment.STATUS_ACTIVE
        ).select_related("user")
        for enrollment in students:
            push_ws_notification(enrollment.user.id, {
                "type": "live_session",
                "title": f"🔴 {session.title} is now LIVE!",
                "session_id": str(session.id),
                "start_time": session.start_time.isoformat(),
            })


@transaction.atomic
def _handle_participant_left(event):
    session = LiveSession.objects.filter(room_name=event.room.name).first()
    if not session:
        return

    user_id = str(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    attendance = LiveSessionAttendance.objects.filter(
        session=session,
        user=user
    ).first()

    if attendance:
        attendance.left_at = timezone.now()
        attendance.save()

    session.last_activity_at = timezone.now()

    if str(session.created_by_id) == user_id:
        # Only set reconnecting if session was LIVE — don't override manual PAUSED
        if session.status != LiveSession.STATUS_PAUSED:
            session.teacher_left_at = timezone.now()
            session.status = LiveSession.STATUS_RECONNECTING
        # If manually paused, teacher just left — keep PAUSED, no timer

    session.save(update_fields=["teacher_left_at",
                 "status", "last_activity_at"])
    broadcast_session_update(session)


def _handle_room_started(event):
    for session in LiveSession.objects.filter(room_name=event.room.name):
        session.status = LiveSession.STATUS_LIVE
        session.save(update_fields=["status"])
        broadcast_session_update(session)


def _handle_room_finished(event):
    for session in LiveSession.objects.filter(room_name=event.room.name):
        # Complete the session regardless of pause state
        if session.status != LiveSession.STATUS_CANCELLED:
            session.status = LiveSession.STATUS_COMPLETED
            session.teacher_left_at = None
            session.save(update_fields=["status", "teacher_left_at"])
            broadcast_session_update(session)
