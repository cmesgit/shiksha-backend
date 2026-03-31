import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from livestream.services import generate_livekit_token  # ← reuse existing

from .models import PrivateSession, SessionParticipant, SessionRescheduleHistory
from .permissions import IsTeacher, IsStudent
from .serializers import (
    SessionListSerializer,
    PrivateSessionSerializer,
    SessionRequestSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ===================================================================
# STUDENT ENDPOINTS
# ===================================================================


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def request_session(request):
    """Student requests a new private session with a teacher."""
    ser = SessionRequestSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data

    try:
        teacher = User.objects.get(pk=d["teacher_id"])
    except User.DoesNotExist:
        return Response({"error": "Teacher not found."}, status=status.HTTP_404_NOT_FOUND)

    if not teacher.has_role("TEACHER"):
        return Response({"error": "Selected user is not a teacher."}, status=status.HTTP_400_BAD_REQUEST)

    session = PrivateSession.objects.create(
        teacher=teacher,
        requested_by=request.user,
        subject=d["subject"],
        scheduled_date=d["scheduled_date"],
        scheduled_time=d["scheduled_time"],
        duration_minutes=d["duration_minutes"],
        session_type=d["session_type"],
        group_strength=d["group_strength"],
        notes=d.get("notes", ""),
        status="pending",
    )

    # Create participant entry for the requesting student
    SessionParticipant.objects.create(session=session, user=request.user, role="student")

    # If group session, add extra student_ids as participants
    for sid in d.get("student_ids", []):
        try:
            extra = User.objects.get(profile__student_id=sid)
            if extra != request.user:
                SessionParticipant.objects.get_or_create(
                    session=session, user=extra, defaults={"role": "student"}
                )
        except User.DoesNotExist:
            pass  # silently skip invalid student IDs

    return Response(PrivateSessionSerializer(session).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsStudent])
def student_sessions(request):
    """
    Return student's sessions filtered by tab query param.
    ?tab=scheduled  → approved / ongoing / needs_reconfirmation
    ?tab=requests   → pending
    ?tab=history    → completed / cancelled / declined / expired / withdrawn / no_show
    """
    tab = request.query_params.get("tab", "scheduled")
    qs = PrivateSession.objects.filter(requested_by=request.user)

    if tab == "scheduled":
        qs = qs.filter(status__in=["approved", "ongoing", "needs_reconfirmation"])
    elif tab == "requests":
        qs = qs.filter(status="pending")
    elif tab == "history":
        qs = qs.filter(
            status__in=[
                "completed", "cancelled", "declined", "expired",
                "withdrawn", "teacher_no_show", "student_no_show",
            ]
        )

    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def cancel_session(request, session_id):
    """Student cancels a pending or approved session."""
    try:
        session = PrivateSession.objects.get(pk=session_id, requested_by=request.user)
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
        session = PrivateSession.objects.get(pk=session_id, requested_by=request.user)
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
        session = PrivateSession.objects.get(pk=session_id, requested_by=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "needs_reconfirmation":
        return Response(
            {"error": "Session is not awaiting reconfirmation."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "declined"
    session.decline_reason = request.data.get("reason", "Student declined reschedule.")
    session.save()
    return Response(PrivateSessionSerializer(session).data)


# ===================================================================
# TEACHER ENDPOINTS
# ===================================================================


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_sessions(request):
    """Teacher's approved / ongoing sessions."""
    qs = PrivateSession.objects.filter(
        teacher=request.user, status__in=["approved", "ongoing"]
    )
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_requests(request):
    """Pending requests awaiting teacher action."""
    qs = PrivateSession.objects.filter(teacher=request.user, status="pending")
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTeacher])
def teacher_history(request):
    """Teacher's completed / cancelled / declined sessions."""
    qs = PrivateSession.objects.filter(
        teacher=request.user,
        status__in=[
            "completed", "cancelled", "declined", "expired",
            "withdrawn", "teacher_no_show", "student_no_show",
        ],
    )
    return Response(SessionListSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def accept_request(request, session_id):
    """Teacher accepts a pending session request."""
    try:
        session = PrivateSession.objects.get(pk=session_id, teacher=request.user)
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
        session = PrivateSession.objects.get(pk=session_id, teacher=request.user)
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
        session = PrivateSession.objects.get(pk=session_id, teacher=request.user)
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
def start_session(request, session_id):
    """
    Teacher starts an approved session.
    Creates a LiveKit room name and marks session as ongoing.
    """
    try:
        session = PrivateSession.objects.get(pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "approved":
        return Response(
            {"error": "Only approved sessions can be started."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    room_name = f"private-{session.id}"
    session.status = "ongoing"
    session.room_name = room_name
    session.started_at = timezone.now()
    session.save()
    return Response(PrivateSessionSerializer(session).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsTeacher])
def end_session(request, session_id):
    """Teacher ends an ongoing session."""
    try:
        session = PrivateSession.objects.get(pk=session_id, teacher=request.user)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "ongoing":
        return Response(
            {"error": "Only ongoing sessions can be ended."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    session.status = "completed"
    session.ended_at = timezone.now()
    session.save()
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
    Reuses livestream.services.generate_livekit_token().

    The existing function expects a session object with .room_name,
    so we pass our PrivateSession directly (it has that field).
    """
    try:
        session = PrivateSession.objects.get(pk=session_id)
    except PrivateSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user

    # Check involvement
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

    try:
        token = generate_livekit_token(
            user=user,
            session=session,       # has .room_name — same interface as LiveSession
            is_teacher=is_teacher,
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