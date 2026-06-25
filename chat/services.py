# PLACEMENT: backend/backend/chat/services.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/services.py
"""
chat/services.py

Helpers shared by the REST views and the websocket consumer. The central idea
is "the active participant": given a request (or ws scope) we resolve WHICH
identity on the account is acting — a specific LearnerProfile or the
TeacherProfile — from the JWT's context + active_profile claims.

This module also owns:
  • BLOCKING  — create/remove/lookup + the permission rule.
  • ROLE LABELS — faculty / guest / academy / skilldev, used by the inbox
    filters and to decide whether a block button may be shown.
  • The MODERATION + BLOCK gate at send time (post_message_checked()).
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerProfile, TeacherProfile
from .models import Conversation, Participant, Message, Block
from . import moderation


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
# Identity helpers
# ---------------------------------------------------------------------------

def _identity_key(kind, obj):
    return f"L:{obj.id}" if kind == Participant.KIND_LEARNER else f"T:{obj.id}"


def identity_key_for_ids(kind, obj_id):
    return f"L:{obj_id}" if kind == Participant.KIND_LEARNER else f"T:{obj_id}"


def resolve_identity(kind, obj_id):
    """Fetch the LearnerProfile / TeacherProfile for a (kind, id) pair, or None."""
    if kind == Participant.KIND_TEACHER:
        return TeacherProfile.objects.filter(id=obj_id).first()
    if kind == Participant.KIND_LEARNER:
        return LearnerProfile.objects.filter(id=obj_id, is_active=True).first()
    return None


# ---------------------------------------------------------------------------
# Conversation creation / lookup
# ---------------------------------------------------------------------------

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


def other_participant(conversation, me_participant):
    """For a DIRECT thread, the single other participant (or None)."""
    return (
        conversation.participants
        .exclude(id=me_participant.id)
        .first()
        if me_participant else None
    )


# ---------------------------------------------------------------------------
# Role labels  (faculty / guest / academy / skilldev)
# ---------------------------------------------------------------------------
#
# A TeacherProfile can be BOTH faculty and guest; a LearnerProfile can be BOTH
# academy and skill-dev. We therefore return a LIST. These labels drive the
# inbox filter tabs and the "primary" label shown on a conversation. They are
# best-effort and never raise — a missing optional app just yields fewer flags.

def _teacher_roles(tp):
    roles = []
    try:
        if tp.academy_status == TeacherProfile.TRACK_APPROVED:
            roles.append("faculty")
    except Exception:
        pass
    is_guest = False
    try:
        if tp.skill_status == TeacherProfile.TRACK_APPROVED:
            is_guest = True
    except Exception:
        pass
    if not is_guest:
        try:
            ep = getattr(tp, "expert_profile", None)
            if ep and ep.is_listed:
                is_guest = True
        except Exception:
            pass
    if is_guest:
        roles.append("guest")
    return roles or ["faculty"]


def _learner_roles(lp):
    roles = []
    skilldev = False
    try:
        from skills.models import SkillSession
        if SkillSession.objects.filter(learner_profile=lp).exists():
            skilldev = True
    except Exception:
        pass
    if not skilldev:
        try:
            from skills.course_models import SkillCourseEnrollment
            if SkillCourseEnrollment.objects.filter(learner_profile=lp).exists():
                skilldev = True
        except Exception:
            pass
    if skilldev:
        roles.append("skilldev")

    academy = False
    try:
        from enrollments.models import Enrollment
        if Enrollment.objects.filter(learner_profile=lp).exists():
            academy = True
    except Exception:
        pass
    if academy:
        roles.append("academy")

    # Every learner has access to the Academy (base track); if we found no
    # signal at all, label them academy so they still appear under a tab.
    return roles or ["academy"]


def participant_roles(participant):
    if participant.kind == Participant.KIND_TEACHER and participant.teacher_profile:
        return _teacher_roles(participant.teacher_profile)
    if participant.kind == Participant.KIND_LEARNER and participant.learner_profile:
        return _learner_roles(participant.learner_profile)
    return []


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------

def can_block(actor_kind, target_kind):
    """Platform rule:
       • TEACHER (faculty/guest expert) may block ANYONE.
       • LEARNER (academy/skill-dev student) may block LEARNERS only.
    """
    if actor_kind == Participant.KIND_TEACHER:
        return True
    if actor_kind == Participant.KIND_LEARNER:
        return target_kind == Participant.KIND_LEARNER
    return False


def _pair_key(a_kind, a_id, b_kind, b_id):
    return f"{identity_key_for_ids(a_kind, a_id)}>{identity_key_for_ids(b_kind, b_id)}"


@transaction.atomic
def create_block(blocker_kind, blocker_obj, blocked_kind, blocked_obj):
    pair = _pair_key(blocker_kind, blocker_obj.id, blocked_kind, blocked_obj.id)
    defaults = dict(blocker_kind=blocker_kind, blocked_kind=blocked_kind)
    if blocker_kind == Participant.KIND_LEARNER:
        defaults["blocker_learner"] = blocker_obj
    else:
        defaults["blocker_teacher"] = blocker_obj
    if blocked_kind == Participant.KIND_LEARNER:
        defaults["blocked_learner"] = blocked_obj
    else:
        defaults["blocked_teacher"] = blocked_obj
    obj, _ = Block.objects.get_or_create(pair_key=pair, defaults=defaults)
    return obj


def remove_block(blocker_kind, blocker_id, blocked_kind, blocked_id):
    pair = _pair_key(blocker_kind, blocker_id, blocked_kind, blocked_id)
    return Block.objects.filter(pair_key=pair).delete()


def has_block(blocker_kind, blocker_id, blocked_kind, blocked_id):
    pair = _pair_key(blocker_kind, blocker_id, blocked_kind, blocked_id)
    return Block.objects.filter(pair_key=pair).exists()


def block_state(a_kind, a_id, b_kind, b_id):
    """Returns (i_blocked_them, they_blocked_me) for actor A vs counterpart B."""
    return (
        has_block(a_kind, a_id, b_kind, b_id),
        has_block(b_kind, b_id, a_kind, a_id),
    )


def is_blocked_between(a_kind, a_id, b_kind, b_id):
    i, they = block_state(a_kind, a_id, b_kind, b_id)
    return i or they


def my_blocks(kind, obj):
    if kind == Participant.KIND_LEARNER:
        return Block.objects.filter(blocker_kind=kind, blocker_learner=obj)
    return Block.objects.filter(blocker_kind=kind, blocker_teacher=obj)


# ---------------------------------------------------------------------------
# Messaging  (raw saver + the moderated/blocked gate)
# ---------------------------------------------------------------------------

@transaction.atomic
def post_message(conversation, sender_participant, body, client_id=""):
    """Raw save. Assumes the body has already passed the gate below."""
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


def post_message_checked(conversation, sender_participant, body, client_id=""):
    """
    The one gate every message passes through.

    Returns (message, error):
      • (msg, None)                      → saved & should be broadcast
      • (None, {"category","reason"})    → refused (moderation or block);
                                            DO NOT broadcast — tell the sender.
      • (None, None)                     → empty body, nothing to do.
    """
    text = (body or "").strip()
    if not text:
        return None, None

    # 1) content moderation (profanity + political/controversial)
    verdict = moderation.check_message(text)
    if not verdict.ok:
        return None, {"category": verdict.category, "reason": verdict.reason}

    # 2) blocking — only meaningful on a 1:1 thread (a course room is a group)
    if conversation.kind == Conversation.KIND_DIRECT:
        other = other_participant(conversation, sender_participant)
        if other is not None:
            a_kind, a_id = sender_participant.kind, _participant_obj_id(sender_participant)
            b_kind, b_id = other.kind, _participant_obj_id(other)
            if is_blocked_between(a_kind, a_id, b_kind, b_id):
                return None, {
                    "category": "blocked",
                    "reason": "Messaging with this person is turned off.",
                }

    msg = post_message(conversation, sender_participant, text, client_id)
    return msg, None


def _participant_obj_id(p):
    return p.learner_profile_id if p.kind == Participant.KIND_LEARNER else p.teacher_profile_id


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


def _participant_dict(p):
    return {
        "id": str(p.id),
        "name": p.display_name(),
        "avatar": p.avatar(),
        "identity": p.identity_key(),
        "kind": p.kind,
        "roles": participant_roles(p),
    }


def serialize_conversation(conv, me_participant=None):
    parts = list(conv.participants.all())
    others = [p for p in parts if not (me_participant and p.id == me_participant.id)]

    unread = 0
    if me_participant and me_participant.last_read_at:
        unread = conv.messages.filter(
            created_at__gt=me_participant.last_read_at
        ).exclude(sender=me_participant).count()
    elif me_participant:
        unread = conv.messages.exclude(sender=me_participant).count()

    last = conv.messages.last()

    # Counterpart + blocking state only apply to DIRECT threads.
    counterpart = None
    blocking = {"i_blocked": False, "blocked_me": False}
    can_block_flag = False
    if conv.kind == Conversation.KIND_DIRECT and me_participant and others:
        cp = others[0]
        counterpart = _participant_dict(cp)
        a_kind, a_id = me_participant.kind, _participant_obj_id(me_participant)
        b_kind, b_id = cp.kind, _participant_obj_id(cp)
        i_blocked, blocked_me = block_state(a_kind, a_id, b_kind, b_id)
        blocking = {"i_blocked": i_blocked, "blocked_me": blocked_me}
        can_block_flag = can_block(me_participant.kind, cp.kind)

    return {
        "id": str(conv.id),
        "kind": conv.kind,
        "title": conv.title or (others[0].display_name() if others else ""),
        "course_id": str(conv.course_id) if conv.course_id else None,
        "participants": [_participant_dict(p) for p in parts],
        "me": (
            {
                "id": str(me_participant.id),
                "identity": me_participant.identity_key(),
                "kind": me_participant.kind,
                "roles": participant_roles(me_participant),
            }
            if me_participant else None
        ),
        "counterpart": counterpart,
        "blocking": blocking,
        "can_block": can_block_flag,
        "last_message": serialize_message(last) if last else None,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "unread": unread,
    }
