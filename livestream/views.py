from django.conf import settings
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.db import transaction

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from livekit import WebhookReceiver

from enrollments.models import Enrollment
from .models import LiveSession, LiveSessionAttendance
from .services import generate_livekit_token
from .serializers import LiveSessionCreateSerializer
import logging

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)
    now = timezone.now()

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session cancelled"}, status=400)

    if now > session.end_time:
        return Response({"detail": "Session ended"}, status=403)

    # STUDENT
    if user.has_role("student"):

        is_enrolled = Enrollment.objects.filter(
            user=user,
            course=session.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled"}, status=403)

        if now < session.start_time - timezone.timedelta(minutes=10):
            return Response({"detail": "Too early to join"}, status=403)

        token = generate_livekit_token(user, session, is_teacher=False)

    # TEACHER
    elif user.has_role("teacher"):

        if not session.subject.teachers.filter(id=user.id).exists():
            return Response({"detail": "Not assigned to this subject"}, status=403)

        token = generate_livekit_token(user, session, is_teacher=True)

    else:
        return Response({"detail": "Unauthorized role"}, status=403)

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
    })


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

        logger.info(f"LiveKit event received: {event.event}")

        if event.event == "participant_joined":
            _handle_participant_join(event)

        elif event.event == "participant_left":
            _handle_participant_left(event)

        elif event.event == "room_started":
            _handle_room_started(event)

        elif event.event == "room_finished":
            _handle_room_finished(event)

        return HttpResponse(status=200)

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return HttpResponse(status=400)


@transaction.atomic
def _handle_participant_join(event):
    room_name = event.room.name
    identity = event.participant.identity

    session = LiveSession.objects.filter(room_name=room_name).first()
    if not session:
        return

    LiveSessionAttendance.objects.update_or_create(
        session=session,
        user_id=identity,
        defaults={"joined_at": timezone.now()}
    )


@transaction.atomic
def _handle_participant_left(event):
    room_name = event.room.name
    identity = event.participant.identity

    session = LiveSession.objects.filter(room_name=room_name).first()
    if not session:
        return

    attendance = LiveSessionAttendance.objects.filter(
        session=session,
        user_id=identity
    ).first()

    if attendance:
        attendance.left_at = timezone.now()
        attendance.save()


def _handle_room_started(event):
    LiveSession.objects.filter(
        room_name=event.room.name
    ).update(status=LiveSession.STATUS_LIVE)


def _handle_room_finished(event):
    LiveSession.objects.filter(
        room_name=event.room.name
    ).update(status=LiveSession.STATUS_COMPLETED)
