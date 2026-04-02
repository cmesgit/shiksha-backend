import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.db import transaction
from django.db.models import Q

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from livekit.api import WebhookReceiver

from enrollments.models import Enrollment
from .models import LiveSession, LiveSessionAttendance
from .services import generate_livekit_token
from .serializers import (
    LiveSessionCreateSerializer,
    LiveSessionListSerializer,
)

logger = logging.getLogger(__name__)


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

        # ✅ course filter
        if course_id:
            queryset = queryset.filter(course_id=course_id)

         # ✅ subject filter (FIXED POSITION)
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
            # 🔐 verify teacher assigned
            if not user.subject_assignments.filter(subject_id=subject_id).exists():
                raise PermissionDenied("Not assigned to this subject.")

            return (
                LiveSession.objects
                .filter(subject_id=subject_id)
                .filter(end_time__gte=cutoff)
                .select_related("course", "subject", "created_by")
                .order_by("start_time")
            )

        # No subject_id — return all sessions across teacher's subjects
        assigned_subject_ids = user.subject_assignments.values_list("subject_id", flat=True)

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

    # 🚨 AUTO EXPIRE (NEW)
    if session.teacher_left_at:
        if now > session.teacher_left_at + timedelta(minutes=10):
            session.status = LiveSession.STATUS_COMPLETED
            session.save()
            return Response(
                {"detail": "Session ended (teacher left)"},
                status=403
            )

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session cancelled"}, status=400)

    if now > session.end_time:
        return Response({"detail": "Session ended"}, status=403)

    # =========================
    # STUDENT
    # =========================
    if user.has_role("STUDENT"):

        is_enrolled = Enrollment.objects.filter(
            user=user,
            course=session.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled"}, status=403)

        if now < session.start_time - timedelta(minutes=10):
            return Response({"detail": "Too early to join"}, status=403)

        is_teacher = False

    # =========================
    # TEACHER
    # =========================
    elif user.has_role("TEACHER"):

        if not session.subject.subject_teachers.filter(teacher=user).exists():
            return Response({"detail": "Not assigned to this subject"}, status=403)

        is_teacher = True

    else:
        return Response({"detail": "Unauthorized role"}, status=403)

    # =========================
    # TOKEN
    # =========================
    try:
        token = generate_livekit_token(
            user=user,
            session=session,
            is_teacher=is_teacher,
        )
    except Exception:
        logger.exception("LiveKit token generation failed")
        return Response({"detail": "LiveKit error"}, status=500)

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": "TEACHER" if is_teacher else "STUDENT",
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

    LiveSessionAttendance.objects.update_or_create(
        session=session,
        user_id=user_id,
        defaults={"joined_at": timezone.now()}
    )

    # 🚨 If teacher joins → reset leave timer
    if session.created_by and str(session.created_by.id) == user_id:
        session.teacher_left_at = None

        # 🚨 mark session LIVE
        if session.status != LiveSession.STATUS_LIVE:
            session.status = LiveSession.STATUS_LIVE

        session.save()


@transaction.atomic
def _handle_participant_left(event):
    session = LiveSession.objects.filter(room_name=event.room.name).first()
    if not session:
        return

    user_id = str(event.participant.identity)

    attendance = LiveSessionAttendance.objects.filter(
        session=session,
        user_id=user_id
    ).first()

    if attendance:
        attendance.left_at = timezone.now()
        attendance.save()

    # 🚨 teacher left → start expiry timer
    if session.created_by and str(session.created_by.id) == user_id:
        session.teacher_left_at = timezone.now()
        session.save()


def _handle_room_started(event):
    LiveSession.objects.filter(
        room_name=event.room.name
    ).update(status=LiveSession.STATUS_LIVE)


def _handle_room_finished(event):
    LiveSession.objects.filter(
        room_name=event.room.name
    ).update(status=LiveSession.STATUS_COMPLETED)
