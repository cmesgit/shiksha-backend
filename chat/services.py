"""
chat/services.py

Helpers shared by the REST views and the websocket consumer. The central idea
is "the active participant": given a request (or ws scope) we resolve WHICH
identity on the account is acting — a specific LearnerProfile or the
TeacherProfile — from the JWT's context + active_profile claims.
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerProfile, TeacherProfile
from .models import Conversation, Participant, Message


# ---------------------------------------------------------------------------
# Resolving the acting identity from JWT context
# ---------------------------------------------------------------------------

def active_identity_from_claims(user, context, active_profile_id):
    """
    Returns (kind, obj) where kind is "LEARNER"/"TEACHER" and obj is the
    LearnerProfile or TeacherProfile, or (None, None) if the account context
    isn't a chat-capable identity.
    """
    if context == "teacher":
        tp = getattr(user, "teacher_profile", None)
        if tp:
            return Participant.KIND_TEACHER, tp
        return None, None

    if context == "learner" and active_profile_id:
        lp = (
            LearnerProfile.objects
            .filter(id=active_profile_id, account=user, is_active=True)
            .first()
        )
        if lp:
            return Participant.KIND_LEARNER, lp

    return None, None


def active_identity_from_request(request):
    token = getattr(request, "auth", None)
    context = token.get("context") if token else None
    active_profile_id = token.get("active_profile") if token else None
    return active_identity_from_claims(request.user, context, active_profile_id)


# ---------------------------------------------------------------------------
# Conversation creation / lookup
# ---------------------------------------------------------------------------

def _identity_key(kind, obj):
    return f"L:{obj.id}" if kind == Participant.KIND_LEARNER else f"T:{obj.id}"


def _attach_participant(conversation, kind, obj):
    if kind == Participant.KIND_LEARNER:
        return Participant.objects.get_or_create(
            conversation=conversation, kind=kind, learner_profile=obj
        )[0]
    return Participant.objects.get_or_create(
        conversation=conversation, kind=kind, teacher_profile=obj
    )[0]


@transaction.atomic
def ensure_direct(a_kind, a_obj, b_kind, b_obj):
    """Get or create the unique 1:1 conversation between two identities."""
    keys = sorted([_identity_key(a_kind, a_obj), _identity_key(b_kind, b_obj)])
    direct_key = "|".join(keys)

    conv, _ = Conversation.objects.get_or_create(
        direct_key=direct_key,
        kind=Conversation.KIND_DIRECT,
    )
    _attach_participant(conv, a_kind, a_obj)
    _attach_participant(conv, b_kind, b_obj)
    return conv


@transaction.atomic
def ensure_course_room(course_id, title=""):
    conv, created = Conversation.objects.get_or_create(
        course_id=course_id,
        kind=Conversation.KIND_COURSE,
        defaults={"title": title},
    )
    if not created and title and conv.title != title:
        conv.title = title
        conv.save(update_fields=["title"])
    return conv


def participant_for(conversation, kind, obj):
    """Return the Participant row for this identity in a conversation, or None."""
    qs = conversation.participants.filter(kind=kind)
    if kind == Participant.KIND_LEARNER:
        return qs.filter(learner_profile=obj).first()
    return qs.filter(teacher_profile=obj).first()


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

@transaction.atomic
def post_message(conversation, sender_participant, body, client_id=""):
    body = (body or "").strip()
    if not body:
        return None

    # Idempotency: if this client_id already exists in the room, return it.
    if client_id:
        existing = conversation.messages.filter(client_id=client_id).first()
        if existing:
            return existing

    msg = Message.objects.create(
        conversation=conversation,
        sender=sender_participant,
        body=body[:4000],
        client_id=client_id,
    )
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at"])
    return msg


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_message(msg):
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "body": msg.body,
        "client_id": msg.client_id,
        "created_at": msg.created_at.isoformat(),
        "sender": {
            "id": str(msg.sender_id) if msg.sender_id else None,
            "name": msg.sender.display_name() if msg.sender else "Unknown",
            "avatar": msg.sender.avatar() if msg.sender else None,
            "identity": msg.sender.identity_key() if msg.sender else None,
        },
    }


def serialize_conversation(conv, me_participant=None):
    others = [p for p in conv.participants.all()
              if not (me_participant and p.id == me_participant.id)]
    unread = 0
    if me_participant and me_participant.last_read_at:
        unread = conv.messages.filter(
            created_at__gt=me_participant.last_read_at
        ).exclude(sender=me_participant).count()
    elif me_participant:
        unread = conv.messages.exclude(sender=me_participant).count()

    last = conv.messages.last()
    return {
        "id": str(conv.id),
        "kind": conv.kind,
        "title": conv.title or (others[0].display_name() if others else ""),
        "course_id": str(conv.course_id) if conv.course_id else None,
        "participants": [
            {
                "id": str(p.id),
                "name": p.display_name(),
                "avatar": p.avatar(),
                "identity": p.identity_key(),
            }
            for p in conv.participants.all()
        ],
        "last_message": serialize_message(last) if last else None,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "unread": unread,
    }
