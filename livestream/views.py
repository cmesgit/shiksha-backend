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
from django.db.models import Q  # ✅ kept (unchanged)
from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model  # ✅ ADDED


from livestream.services.session_state import set_session_state


def broadcast_session_update(session):
    channel_layer = get_channel_layer()

    # 🔥 NEW: update Redis (safe)
    try:
        set_session_state(session)
    except Exception:
        pass  # never break system if Redis fails

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
        assigned_subject_ids = user.subject_assignments.values_list(
            "subject_id", flat=True)

        return (
            LiveSession.objects
            .filter(subject_id__in=assigned_subject_ids)
            .filter(end_time__gte=cutoff)
            .select_related("course", "subject", "created_by")
            .order_by("start_time")
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)
    now = timezone.now()

    # 🚫 CANCELLED
    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session cancelled"}, status=400)

    # ==================================================
    # 🔥 TEACHER DISCONNECT / EXPIRY LOGIC (CENTRALIZED)
    # ==================================================
    if session.teacher_left_at:
        diff = now - session.teacher_left_at

        # ❌ permanently ended
        if diff > timedelta(minutes=60):
            if session.status != LiveSession.STATUS_COMPLETED:
                session.status = LiveSession.STATUS_COMPLETED
                session.save(update_fields=["status"])  # ✅ FIX

            return Response(
                {"detail": "Session permanently ended"},
                status=403
            )

    # =========================
    # 👨‍🎓 STUDENT
    # =========================
    if user.has_role("STUDENT"):

        # ✅ enrollment check FIRST
        is_enrolled = Enrollment.objects.filter(
            user=user,
            course=session.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled"}, status=403)

        # 🔥 early join restriction
        if now < session.start_time - timedelta(minutes=15):
            return Response({"detail": "Too early"}, status=403)

        # 🔥 optional: block if teacher gone too long
        if session.teacher_left_at:
            diff = now - session.teacher_left_at

            if diff > timedelta(minutes=60):
                return Response(
                    {"detail": "Session ended"},
                    status=403
                )

        is_teacher = False

    # =========================
    # 👨‍🏫 TEACHER
    # =========================
    elif user.has_role("TEACHER"):

        if not session.subject.subject_teachers.filter(teacher=user).exists():
            return Response({"detail": "Not assigned"}, status=403)

        # 🔥 ONLY CREATOR IS PRESENTER
        is_creator = str(session.created_by_id) == str(user.id)
        is_teacher = is_creator  # presenter only if creator

        # 🔥 REVIVE SESSION (only creator matters)
        if is_creator and session.teacher_left_at:
            if now <= session.teacher_left_at + timedelta(minutes=30):
                session.teacher_left_at = None
                session.status = LiveSession.STATUS_LIVE
                session.save(update_fields=["teacher_left_at", "status"])

    else:
        return Response({"detail": "Unauthorized"}, status=403)

    # =========================
    # 🔐 TOKEN
    # =========================
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

    session.status = LiveSession.STATUS_CANCELLED
    session.save(update_fields=["status"])  # ✅ FIX

    return Response({"detail": "Session cancelled successfully."})


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

    User = get_user_model()  # ✅ FIX
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    LiveSessionAttendance.objects.update_or_create(
        session=session,
        user=user,
        defaults={"joined_at": timezone.now()}
    )

    session.last_activity_at = timezone.now()

    # 🔥 TEACHER JOIN
    if str(session.created_by_id) == user_id:
        session.teacher_left_at = None
        session.status = LiveSession.STATUS_LIVE

    session.save(update_fields=["teacher_left_at",
                 "status", "last_activity_at"])  # ✅ FIX

    broadcast_session_update(session)


@transaction.atomic
def _handle_participant_left(event):
    session = LiveSession.objects.filter(room_name=event.room.name).first()
    if not session:
        return

    user_id = str(event.participant.identity)

    User = get_user_model()  # ✅ FIX
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

    # 🔥 TEACHER LEFT (network OR actual)
    if str(session.created_by_id) == user_id:
        session.teacher_left_at = timezone.now()
        session.status = LiveSession.STATUS_RECONNECTING

    session.save(update_fields=["teacher_left_at",
                 "status", "last_activity_at"])  # ✅ FIX

    broadcast_session_update(session)


def _handle_room_started(event):
    sessions = LiveSession.objects.filter(room_name=event.room.name)

    for session in sessions:
        session.status = LiveSession.STATUS_LIVE
        session.save(update_fields=["status"])  # ✅ FIX
        broadcast_session_update(session)


def _handle_room_finished(event):
    sessions = LiveSession.objects.filter(room_name=event.room.name)

    for session in sessions:
        session.status = LiveSession.STATUS_COMPLETED
        session.save(update_fields=["status"])  # ✅ FIX
        broadcast_session_update(session)
