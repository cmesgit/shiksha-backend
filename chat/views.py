"""
chat/views.py — REST endpoints (history, conversation list, starting chats).
Live delivery is over the websocket (see consumers.py); REST covers the rest.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework import status
from django.utils import timezone
from django.db.models import Q

from accounts.models import LearnerProfile, TeacherProfile
from .models import Conversation, Participant, Message
from . import services


def _require_identity(request):
    kind, obj = services.active_identity_from_request(request)
    if not kind:
        raise PermissionDenied(
            "Select a learner profile or enter teacher mode before using chat."
        )
    return kind, obj


def _my_conversations(kind, obj):
    if kind == Participant.KIND_LEARNER:
        part_q = Q(participants__kind=kind, participants__learner_profile=obj)
    else:
        part_q = Q(participants__kind=kind, participants__teacher_profile=obj)
    return Conversation.objects.filter(part_q).distinct()


class ConversationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        convs = _my_conversations(kind, obj).prefetch_related("participants", "messages")
        out = []
        for c in convs:
            me = services.participant_for(c, kind, obj)
            out.append(services.serialize_conversation(c, me))
        return Response(out)


class StartDirectView(APIView):
    """
    POST { target_kind: "TEACHER"|"LEARNER", target_id }
    Starts (or returns) a 1:1 conversation between the active identity and
    the target identity.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind, obj = _require_identity(request)
        target_kind = request.data.get("target_kind")
        target_id = request.data.get("target_id")
        if target_kind not in (Participant.KIND_LEARNER, Participant.KIND_TEACHER):
            raise ValidationError({"target_kind": "Must be LEARNER or TEACHER."})

        if target_kind == Participant.KIND_TEACHER:
            target = TeacherProfile.objects.filter(id=target_id).first()
        else:
            target = LearnerProfile.objects.filter(id=target_id, is_active=True).first()
        if not target:
            raise ValidationError({"target_id": "Target not found."})

        conv = services.ensure_direct(kind, obj, target_kind, target)
        me = services.participant_for(conv, kind, obj)
        return Response(
            services.serialize_conversation(conv, me),
            status=status.HTTP_201_CREATED,
        )


class CourseRoomView(APIView):
    """
    POST { course_id, title? }  — join (or create) the course's group room.
    Membership check is delegated to the caller's app context; here we simply
    attach the active identity as a participant.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind, obj = _require_identity(request)
        course_id = request.data.get("course_id")
        if not course_id:
            raise ValidationError({"course_id": "Required."})
        conv = services.ensure_course_room(course_id, request.data.get("title", ""))
        services._attach_participant(conv, kind, obj)
        me = services.participant_for(conv, kind, obj)
        return Response(services.serialize_conversation(conv, me))


class MessageListView(APIView):
    """GET /chat/conversations/<id>/messages/?before=<iso>&limit=50"""
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        kind, obj = _require_identity(request)
        conv = Conversation.objects.filter(id=conversation_id).first()
        if not conv:
            raise ValidationError("Conversation not found.")
        me = services.participant_for(conv, kind, obj)
        if not me:
            raise PermissionDenied("You are not a participant in this conversation.")

        qs = conv.messages.all()
        before = request.query_params.get("before")
        if before:
            qs = qs.filter(created_at__lt=before)
        try:
            limit = min(int(request.query_params.get("limit", 50)), 100)
        except ValueError:
            limit = 50
        msgs = list(qs.order_by("-created_at")[:limit])[::-1]
        return Response([services.serialize_message(m) for m in msgs])


class MarkReadView(APIView):
    """POST /chat/conversations/<id>/read/ — set last_read_at = now."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        kind, obj = _require_identity(request)
        conv = Conversation.objects.filter(id=conversation_id).first()
        if not conv:
            raise ValidationError("Conversation not found.")
        me = services.participant_for(conv, kind, obj)
        if not me:
            raise PermissionDenied("Not a participant.")
        me.last_read_at = timezone.now()
        me.save(update_fields=["last_read_at"])
        return Response({"detail": "ok", "last_read_at": me.last_read_at.isoformat()})
