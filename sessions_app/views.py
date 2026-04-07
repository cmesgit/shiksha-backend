from django.utils.timezone import make_aware
from datetime import datetime, timedelta
import logging

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from sessions_app.services.private_token import generate_private_token

from .models import PrivateSession, SessionParticipant, SessionRescheduleHistory, ChatMessage
from .permissions import IsTeacher, IsStudent
from .serializers import (
    SessionListSerializer,
    PrivateSessionSerializer,
    SessionRequestSerializer,
    ChatMessageSerializer,
    get_user_name,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ===================================================================
# STUDENT ENDPOINTS
# ===================================================================


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def request_session(request):
    ser = SessionRequestSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data

    from courses.models import Subject, SubjectTeacher

    # ✅ Validate subject
    try:
        subject_obj = Subject.objects.get(id=d["subject_id"])
    except Subject.DoesNotExist:
        return Response({"error": "Invalid subject"}, status=400)

    # ✅ Validate teacher teaches subject
    if not SubjectTeacher.objects.filter(
        subject=subject_obj,
        teacher_id=d["teacher_id"]
    ).exists():
        return Response(
            {"error": "Teacher does not teach this subject"},
            status=400
        )

    # ✅ Get teacher
    try:
        teacher = User.objects.get(pk=d["teacher_id"])
    except User.DoesNotExist:
        return Response({"error": "Teacher not found"}, status=404)

    # ✅ Create session
    session = PrivateSession.objects.create(
        teacher=teacher,
        requested_by=request.user,
        subject=subject_obj.name,  # store name
        scheduled_date=d["scheduled_date"],
        scheduled_time=d["scheduled_time"],
        duration_minutes=d["duration_minutes"],
        session_type=d["session_type"],
        group_strength=d["group_strength"],
        notes=d.get("notes", ""),
        status="pending",
    )

    # ✅ Add requester
    SessionParticipant.objects.create(
        session=session,
        user=request.user,
        role="student"
    )

    return Response(
        PrivateSessionSerializer(session).data,
        status=201
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsStudent])
def student_sessions(request):
    """
    Return student's sessions filtered by tab query param.
    Includes sessions where student is requester OR a group participant.

    ?tab=scheduled  → approved / ongoing / needs_reconfirmation
    ?tab=requests   → pending
    ?tab=history    → completed / cancelled / declined / expired / withdrawn / no_show
    ?search=keyword → filter by subject, teacher name, or student name
    """
    tab = request.query_params.get("tab", "scheduled")
    search = request.query_params.get("search", "").strip()
    user = request.user

    # Sessions where user is requester OR a participant
    qs = PrivateSession.objects.filter(
        Q(requested_by=user) | Q(participants__user=user)
    ).distinct()

    if tab == "scheduled":
        qs = qs.filter(
            status__in=["approved", "ongoing", "needs_reconfirmation"])
    elif tab == "requests":
        qs = qs.filter(status="pending")
    elif tab == "history":
        qs = qs.filter(
            status__in=[
                "completed", "cancelled", "declined", "expired",
                "withdrawn", "teacher_no_show", "student_no_show",
            ]
        )

    # Search filter
    if search:
        qs = qs.filter(
            Q(subject__icontains=search)
            | Q(teacher__profile__full_name__icontains=search)
            | Q(teacher__username__icontains=search)
            | Q(requested_by__profile__full_name__icontains=search)
            | Q(requested_by__username__icontains=search)
        ).distinct()

    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def cancel_session(request, session_id):
    """Student cancels a pending or approved session they requested."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, requested_by=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status not in ("pending", "approved", "needs_reconfirmation"):
        return Response(
            {"error": f"Cannot cancel a session with status '{session.status}'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "cancelled"
    session.cancel_reason = request.data.get("reason", "")
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def confirm_reschedule(request, session_id):
    """Student confirms a teacher's reschedule proposal."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, requested_by=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "needs_reconfirmation":
        return Response(
            {"error": "Session is not awaiting reconfirmation."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.scheduled_date = session.rescheduled_date
    session.scheduled_time = session.rescheduled_time
    session.rescheduled_date = None
    session.rescheduled_time = None
    session.status = "approved"
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def decline_reschedule(request, session_id):
    """Student declines a teacher's reschedule proposal → session is declined."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, requested_by=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "needs_reconfirmation":
        return Response(
            {"error": "Session is not awaiting reconfirmation."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "declined"
    session.decline_reason = request.data.get(
        "reason", "Student declined reschedule.")
    session.save()
    return Response(PrivateSessionSerializer(session).data)


# ===================================================================
# TEACHER ENDPOINTS
# ===================================================================


def _apply_search(qs, search):
    """Apply search filter across subject, teacher name, and student name."""
    if not search:
        return qs
    return qs.filter(
        Q(subject__icontains=search)
        | Q(teacher__profile__full_name__icontains=search)
        | Q(teacher__username__icontains=search)
        | Q(requested_by__profile__full_name__icontains=search)
        | Q(requested_by__username__icontains=search)
    ).distinct()


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_sessions(request):
    """Teacher's approved / ongoing sessions. Supports ?search= query param."""
    search = request.query_params.get("search", "").strip()
    qs = PrivateSession.objects.filter(
        teacher=request.user, status__in=["approved", "ongoing"]
    )
    qs = _apply_search(qs, search)
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_requests(request):
    """Pending requests awaiting teacher action. Supports ?search= query param."""
    search = request.query_params.get("search", "").strip()
    qs = PrivateSession.objects.filter(teacher=request.user, status="pending")
    qs = _apply_search(qs, search)
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_history(request):
    """Teacher's completed / cancelled / declined sessions. Supports ?search= query param."""
    search = request.query_params.get("search", "").strip()
    qs = PrivateSession.objects.filter(
        teacher=request.user,
        status__in=[
            "completed", "cancelled", "declined", "expired",
            "withdrawn", "teacher_no_show", "student_no_show",
        ],
    )
    qs = _apply_search(qs, search)
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def accept_request(request, session_id):
    """Teacher accepts a pending session request."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "pending":
        return Response(
            {"error": "Only pending requests can be accepted."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    new_date = request.data.get("scheduled_date")
    new_time = request.data.get("scheduled_time")
    if new_date:
        session.scheduled_date = new_date
    if new_time:
        session.scheduled_time = new_time

    session.status = "approved"
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def decline_request(request, session_id):
    """Teacher declines a pending session request."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "pending":
        return Response(
            {"error": "Only pending requests can be declined."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "declined"
    session.decline_reason = request.data.get("reason", "")
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def reschedule_request(request, session_id):
    """Teacher proposes a new date/time for a pending or approved session."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status not in ("pending", "approved"):
        return Response(
            {"error": "Cannot reschedule a session with this status."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    new_date = request.data.get("scheduled_date")
    new_time = request.data.get("scheduled_time")
    reason = request.data.get("reason", "")

    if not new_date or not new_time:
        return Response(
            {"error": "scheduled_date and scheduled_time are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    SessionRescheduleHistory.objects.create(
        session=session,
        proposed_by=request.user,
        original_date=session.scheduled_date,
        original_time=session.scheduled_time,
        proposed_date=new_date,
        proposed_time=new_time,
        reason=reason,
    )

    session.rescheduled_date = new_date
    session.rescheduled_time = new_time
    session.reschedule_reason = reason
    session.status = "needs_reconfirmation"
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_cancel_session(request, session_id):
    """Teacher cancels a pending, approved, or needs_reconfirmation session."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status not in ("pending", "approved", "needs_reconfirmation"):
        return Response(
            {"error": f"Cannot cancel a session with status '{session.status}'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "cancelled"
    session.cancel_reason = request.data.get("reason", "Cancelled by teacher.")
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def start_session(request, session_id):
    """
    Teacher starts an approved session.
    Creates a LiveKit room name and marks session as ongoing.
    """
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "approved":
        return Response(
            {"error": "Only approved sessions can be started."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "ongoing"
    session.room_name = f"private_{session.id}"
    session.started_at = timezone.now()
    session.active_connections = 0
    session.all_left_at = None
    session.save()
    return Response(PrivateSessionSerializer(session).data)


def _end_session_internal(session, reason="ended"):
    """
    Shared helper to mark a session as completed and clean up.
    Called by the teacher's end_session view AND by the auto-expire logic.
    Returns True if the session was ended, False if it was already completed.
    """
    if session.status != "ongoing":
        return False

    session.status = "completed"
    session.ended_at = timezone.now()
    session.save()

    # Clean up chat messages — no longer needed after session ends
    ChatMessage.objects.filter(session=session).delete()

    logger.info(
        "Session %s %s (reason: %s)",
        session.id,
        "completed",
        reason,
    )
    return True


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def end_session(request, session_id):
    """Teacher ends an ongoing session."""
    try:
        session = PrivateSession.objects.get(
            pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "ongoing":
        return Response(
            {"error": "Only ongoing sessions can be ended."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    _end_session_internal(session, reason="teacher_ended")

    return Response(PrivateSessionSerializer(session).data)


# ===================================================================
# SHARED ENDPOINTS
# ===================================================================


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session_detail(request, session_id):
    """Return full session detail. Accessible by teacher, student, or participant."""
    try:
        session = PrivateSession.objects.select_related(
            "teacher", "teacher__profile",
            "requested_by", "requested_by__profile",
        ).prefetch_related("participants__user__profile").get(pk=session_id)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user
    is_involved = (
        session.teacher == user
        or session.requested_by == user
        or session.participants.filter(user=user).exists()
    )
    if not is_involved:
        return Response(
            {"error": "You do not have access to this session."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_private_session(request, session_id):
    """
    Get a LiveKit token to join a private session.
    Both teacher and students can publish audio/video in private sessions.
    """
    try:
        session = PrivateSession.objects.get(pk=session_id)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user

    is_teacher = (session.teacher == user)
    is_student = (
        session.requested_by == user
        or session.participants.filter(user=user).exists()
    )

    if not is_teacher and not is_student:
        return Response(
            {"error": "You are not a participant in this session."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if session.status != "ongoing":
        return Response(
            {"error": "Session is not currently ongoing."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not session.room_name:
        return Response(
            {"error": "No room has been created for this session yet."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Use display name from profile (not AbstractUser's first/last)
    display_name = get_user_name(user)

    try:
        token = generate_private_token(
            user=user,
            session=session,
            display_name=display_name,
        )
    except Exception:
        logger.exception("LiveKit token generation failed for private session")
        return Response({"detail": "LiveKit error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Track join time for participants
    participant = session.participants.filter(user=user).first()
    if participant and not participant.joined_at:
        participant.joined_at = timezone.now()
        participant.save(update_fields=["joined_at"])

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": "TEACHER" if is_teacher else "STUDENT",
    })


# ===================================================================
# CHAT ENDPOINTS
# ===================================================================


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session_chat_messages(request, session_id):
    """Retrieve all chat messages for a private session."""
    try:
        session = PrivateSession.objects.get(pk=session_id)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user
    is_involved = (
        session.teacher == user
        or session.requested_by == user
        or session.participants.filter(user=user).exists()
    )
    if not is_involved:
        return Response({"error": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

    messages = ChatMessage.objects.filter(
        session=session
    ).order_by("created_at")[:200]
    return Response(ChatMessageSerializer(messages, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_chat_message(request, session_id):
    """Send a chat message in a private session. Persists to DB and broadcasts via channels."""
    try:
        session = PrivateSession.objects.get(pk=session_id)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user
    is_teacher = (session.teacher == user)
    is_student = (
        session.requested_by == user
        or session.participants.filter(user=user).exists()
    )

    if not is_teacher and not is_student:
        return Response({"error": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

    if session.status != "ongoing":
        return Response({"error": "Chat is only available for active sessions."}, status=status.HTTP_400_BAD_REQUEST)

    message_text = request.data.get("message", "").strip()
    if not message_text:
        return Response({"error": "Message cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)

    display_name = get_user_name(user)
    role = "teacher" if is_teacher else "student"

    chat_msg = ChatMessage.objects.create(
        session=session,
        sender=user,
        sender_name=display_name,
        sender_role=role,
        message=message_text,
    )

    # Broadcast to all WebSocket clients in this session's chat group
    serialized = ChatMessageSerializer(chat_msg).data
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"private_session_chat_{session_id}",
        {
            "type": "chat_message",
            "data": serialized,
        },
    )

    return Response(serialized, status=status.HTTP_201_CREATED)


# ==========================================================
# SUBJECT → AVAILABLE TEACHERS
# ==========================================================


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def subject_teachers(request, subject_id):
    """
    Get teachers for a subject.
    Optional: filters out busy teachers.
    """
    from courses.models import SubjectTeacher

    date = request.query_params.get("date")
    time = request.query_params.get("time")
    duration = int(request.query_params.get("duration", 60))

    qs = SubjectTeacher.objects.filter(
        subject_id=subject_id
    ).select_related("teacher", "teacher__profile")

    # 🔥 Availability filtering (optional but included)
    if date and time:
        try:
            start = make_aware(datetime.strptime(
                f"{date} {time}", "%Y-%m-%d %H:%M"))
            end = start + timedelta(minutes=duration)

            busy_teachers = PrivateSession.objects.filter(
                status__in=["approved", "ongoing"],
                scheduled_date=date
            ).values_list("teacher_id", flat=True)

            qs = qs.exclude(teacher_id__in=busy_teachers)

        except Exception:
            pass

    data = [
        {
            "id": str(st.teacher.id),
            "name": getattr(st.teacher.profile, "full_name", st.teacher.username),
        }
        for st in qs
    ]

    return Response(data)
