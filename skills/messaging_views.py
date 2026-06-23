"""
skills/messaging_views.py
─────────────────────────
Learner → Expert direct messaging backed by the existing
Conversation + Message models in skills/models.py.

Routes (skills/urls.py):
  Learner
    GET/POST /skill/conversations/              → my threads, start thread
    GET      /skill/conversations/<id>/         → thread detail + messages
    POST     /skill/conversations/<id>/messages/ → send message

  Teacher
    GET      /skill/teacher/inbox/              → all conversations received
    POST     /skill/conversations/<id>/messages/ → same endpoint, reply in thread

Unread count: messages where sender ≠ reader and read_at IS NULL.
"""
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework import status

from accounts.auth_flow import get_active_profile
from .models import ExpertProfile, Conversation, Message


def _conv_summary(conv, viewer_user):
    """Serialise a conversation for list views."""
    last = conv.messages.order_by("-created_at").first()
    unread = conv.messages.filter(read_at__isnull=True).exclude(sender=viewer_user).count()
    expert = conv.expert
    learner = conv.learner_profile
    return {
        "id":      str(conv.id),
        "updated_at": conv.updated_at,
        "unread":  unread,
        "last_message": {
            "body":       last.body if last else None,
            "created_at": last.created_at if last else None,
            "from_me":    last.sender_id == viewer_user.id if last else None,
        },
        "expert": {
            "id":       str(expert.id),
            "name":     expert.display_name(),
            "headline": expert.headline,
        },
        "learner": {
            "id":   str(learner.id),
            "name": learner.display_name or learner.full_name or "Student",
        },
    }


def _msg_payload(msg, viewer_user):
    return {
        "id":         str(msg.id),
        "body":       msg.body,
        "created_at": msg.created_at,
        "from_me":    msg.sender_id == viewer_user.id,
        "sender_name": msg.sender.email,
        "read_at":    msg.read_at,
    }


def _mark_read(conv, reader):
    """Mark all messages not sent by reader as read."""
    now = timezone.now()
    conv.messages.filter(read_at__isnull=True).exclude(sender=reader).update(read_at=now)


class ConversationListCreateView(APIView):
    """
    GET  → learner's conversations (ordered by most recent).
    POST { expert } → open or fetch an existing thread, returns the full
         conversation + messages so the client can open it immediately.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")
        convs = (
            Conversation.objects
            .filter(learner_profile=learner)
            .select_related("expert", "learner_profile")
            .prefetch_related("messages")
            .order_by("-updated_at")
        )
        return Response([_conv_summary(c, request.user) for c in convs])

    def post(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        expert_id = request.data.get("expert")
        if not expert_id:
            raise ValidationError({"expert": "Required."})
        expert = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not expert:
            raise NotFound("Expert not found.")

        conv, created = Conversation.objects.get_or_create(
            learner_profile=learner, expert=expert
        )
        msgs = conv.messages.order_by("created_at")
        return Response(
            {
                **_conv_summary(conv, request.user),
                "messages": [_msg_payload(m, request.user) for m in msgs],
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ConversationDetailView(APIView):
    """GET the full thread (marks messages as read)."""
    permission_classes = [IsAuthenticated]

    def _get_conv(self, user, conv_id):
        # Both the learner and the expert teacher can access the thread.
        conv = (
            Conversation.objects
            .filter(id=conv_id)
            .select_related("expert__teacher_profile__user", "learner_profile__account")
            .prefetch_related("messages")
            .first()
        )
        if not conv:
            raise NotFound("Conversation not found.")
        is_learner = conv.learner_profile.account_id == user.id
        is_expert  = conv.expert.teacher_profile.user_id == user.id
        if not is_learner and not is_expert:
            raise PermissionDenied("Not a participant.")
        return conv

    def get(self, request, conv_id):
        conv = self._get_conv(request.user, conv_id)
        _mark_read(conv, request.user)
        msgs = conv.messages.order_by("created_at")
        return Response({
            **_conv_summary(conv, request.user),
            "messages": [_msg_payload(m, request.user) for m in msgs],
        })


class MessageSendView(APIView):
    """POST { body } → send a message in the thread."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conv_id):
        conv = (
            Conversation.objects
            .filter(id=conv_id)
            .select_related("expert__teacher_profile__user", "learner_profile__account")
            .first()
        )
        if not conv:
            raise NotFound("Conversation not found.")
        is_learner = conv.learner_profile.account_id == request.user.id
        is_expert  = conv.expert.teacher_profile.user_id == request.user.id
        if not is_learner and not is_expert:
            raise PermissionDenied("Not a participant.")

        body = (request.data.get("body") or "").strip()
        if not body:
            raise ValidationError({"body": "Message body required."})

        msg = Message.objects.create(conversation=conv, sender=request.user, body=body)
        # Bump conversation timestamp so it rises in lists.
        conv.updated_at = timezone.now()
        conv.save(update_fields=["updated_at"])

        return Response(_msg_payload(msg, request.user), status=status.HTTP_201_CREATED)


class TeacherInboxView(APIView):
    """GET → all conversations received by this teacher's expert profile."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        expert = ExpertProfile.objects.filter(
            teacher_profile__user=request.user
        ).first()
        if not expert:
            raise PermissionDenied("No expert profile.")
        convs = (
            Conversation.objects
            .filter(expert=expert)
            .select_related("expert", "learner_profile")
            .prefetch_related("messages")
            .order_by("-updated_at")
        )
        return Response([_conv_summary(c, request.user) for c in convs])
