# PLACEMENT: backend/backend/chat/services.py   (FULL FILE — REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/services.py
#
# M3 changes (Phase 3 §9/§10/§11), on top of everything below:
#   • ensure_course_room() is now a thin wrapper over the new, generalized
#     ensure_room(context_type, context_id, title). Same signature/behaviour
#     for its one existing caller (chat/views.py's CourseRoomView).
#   • Two new lookups backing chat/policy.py's DM matrix:
#     learner_teacher_share_active_course() and learners_share_room().
#   • post_message_checked() gains policy.can_post() as a structural check
#     BEFORE moderation (frozen conversation / read-only broadcast). The
#     existing moderation + block checks are untouched, same order as before
#     relative to each other.
#   • post_message() writes an OutboxEvent in the SAME transaction as the
#     Message it's about (Phase 3 §11) — chat/outbox_handlers.py drains it.
#   • serialize_conversation()/course_room_track() updated for kind=ROOM +
#     context_type/context_id replacing kind=COURSE + course_id. No response
#     shape change for callers: `course_id` in the serialized dict is still
#     a course id string (or None), same as before.
#
# PLACEMENT: backend/backend/chat/services.py   (FULL FILE — REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/services.py
#
# This is your original services.py with ONE function changed:
# learner_in_course() now gates class-chat on the live subscription (the same
# rule that gates course content) instead of the Enrollment row, which stayed
# ACTIVE after a subscription expired. Everything else is byte-identical.
# Replaces the one-function note from patch set 3. No migration needed.

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
  • The MODERATION + BLOCK + POLICY gate at send time (post_message_checked()).
  • ROOM creation/lookup, generalized across context_type (Phase 3 §9).
  • The lookups chat/policy.py's DM matrix needs — this module still owns
    every actual DB query; policy.py owns only the yes/no decision logic.
"""
from django.db import transaction
from django.utils import timezone
import logging

from accounts.models import LearnerProfile, TeacherProfile, Identity
from .models import Conversation, Participant, Message, Block, OutboxEvent
from . import moderation
from . import redis_utils
from . import realtime
from . import policy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolving the acting identity from JWT context
# ---------------------------------------------------------------------------

def _resolve_via_identity_claim(user, identity_claim):
    """M1 dual-read fast path (Phase 3 §7): parse kind+profile_id straight
    from the new `identity` JWT claim instead of re-deriving it from
    context. Returns (None, None) on ANY failure — absent claim, malformed
    string, or an id that doesn't validate against this user — so the
    caller always has a safe path back to the original logic. This must
    never be the only way a request can succeed."""
    if not identity_claim or ":" not in identity_claim:
        return None, None
    kind_letter, _, profile_id = identity_claim.partition(":")

    if kind_letter == "L":
        lp = (
            LearnerProfile.objects
            .filter(id=profile_id, account=user, is_active=True)
            .first()
        )
        if lp:
            return Participant.KIND_LEARNER, lp
    elif kind_letter == "T":
        tp = getattr(user, "teacher_profile", None)
        if tp and str(tp.id) == str(profile_id):
            return Participant.KIND_TEACHER, tp

    return None, None


def active_identity_from_claims(user, context, active_profile_id, identity_claim=None):
    """
    Returns (kind, obj) where kind is "LEARNER"/"TEACHER" and obj is the
    LearnerProfile or TeacherProfile, or (None, None) if the account context
    isn't a chat-capable identity.

    M1 (Phase 3 §7): `identity_claim` is optional and additive. Tokens
    minted before this deploy simply won't carry it — every existing call
    site keeps working unchanged (the new parameter defaults to None). When
    present, it's tried FIRST as a single-claim fast path; on any failure
    (absent, malformed, or fails ownership validation) this falls through to
    the original context + active_profile_id logic below, unchanged from
    before M1. debug-level logs here are the "dual-read verified in logs"
    signal for the M1 rollout window (Phase 3 §33 DoD) — quiet by default
    given this project's ERROR-level root logger; bump this logger's level
    temporarily to see the old-path/new-path split.
    """
    if identity_claim:
        kind, obj = _resolve_via_identity_claim(user, identity_claim)
        if kind:
            logger.debug("chat.services: identity resolved via M1 claim (%s)", identity_claim)
            return kind, obj
        logger.debug(
            "chat.services: identity claim %r present but did not validate — "
            "falling back to legacy context/active_profile resolution",
            identity_claim,
        )

    if context == "teacher":
        tp = getattr(user, "teacher_profile", None)
        if tp:
            logger.debug("chat.services: identity resolved via legacy context=teacher path")
            return Participant.KIND_TEACHER, tp
        return None, None

    if context == "learner" and active_profile_id:
        lp = (
            LearnerProfile.objects
            .filter(id=active_profile_id, account=user, is_active=True)
            .first()
        )
        if lp:
            logger.debug("chat.services: identity resolved via legacy context=learner path")
            return Participant.KIND_LEARNER, lp

    return None, None


def active_identity_from_request(request):
    token = getattr(request, "auth", None)
    context = token.get("context") if token else None
    active_profile_id = token.get("active_profile") if token else None
    identity_claim = token.get("identity") if token else None
    return active_identity_from_claims(request.user, context, active_profile_id, identity_claim)


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

def _get_identity(kind, obj):
    """M1 dual-write helper: look up the registry row for this (kind, obj).
    Read-only — the Identity row itself is always created by
    accounts/signals.py on profile save, never here. If it's somehow
    missing (e.g. a signal failed, or this runs mid-backfill), returns None
    and the caller just leaves the new FK unset; the polymorphic columns
    remain correct either way, since they're still the source of truth
    throughout M1."""
    try:
        letter = Identity.kind_for_participant_kind(kind)
        return Identity.objects.filter(kind=letter, profile_id=str(obj.id)).first()
    except Exception:
        logger.exception(
            "chat.services: identity lookup failed for kind=%s obj_id=%s",
            kind, getattr(obj, "id", None),
        )
        return None


def _attach_participant(conversation, kind, obj):
    # M1: `defaults` only apply on CREATE, never touching an existing row —
    # correct, since a pre-M1 row's identity FK is handled once by the
    # 0005_populate_identity_fk backfill migration, not by re-running this.
    identity = _get_identity(kind, obj)
    if kind == Participant.KIND_LEARNER:
        return Participant.objects.get_or_create(
            conversation=conversation, kind=kind, learner_profile=obj,
            defaults={"identity": identity},
        )[0]
    return Participant.objects.get_or_create(
        conversation=conversation, kind=kind, teacher_profile=obj,
        defaults={"identity": identity},
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
def ensure_room(context_type, context_id, title=""):
    """Get or create the unique ROOM conversation for (context_type,
    context_id). Generalizes what was ensure_course_room()'s body (Phase 3
    §9) — the actual behaviour (get-or-create by context, keep title in
    sync on an existing room) is unchanged, only the key it get-or-creates
    on has widened from a single course_id to (context_type, context_id).
    `context_id` is stored as-is; callers are responsible for str()'ing a
    UUID (or leaving an int id as its natural str()) before calling this —
    see ensure_course_room()'s str(course_id) below.
    """
    conv, created = Conversation.objects.get_or_create(
        context_type=context_type,
        context_id=context_id,
        kind=Conversation.KIND_ROOM,
        defaults={"title": title},
    )
    if not created and title and conv.title != title:
        conv.title = title
        conv.save(update_fields=["title"])
    return conv


def ensure_course_room(course_id, title=""):
    """Thin wrapper over ensure_room() — kept so CourseRoomView (chat's own
    one caller) doesn't need to change at all. context_id is a CharField
    (Phase 3 §9 — see the Conversation.context_id field comment for why),
    so the UUID is str()'d here, once, at the one call site that still
    deals in raw course ids."""
    return ensure_room("course", str(course_id), title)


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
# Course-room membership  (who may join a course's ROOM, context_type="course")
# ---------------------------------------------------------------------------
#
# A per-course group room. Members are:
#   • LEARNERS with an ACTIVE enrollment in that course, and
#   • TEACHERS who teach at least one subject of that course.
# Membership accretes as people open the room (the room shows the full message
# history regardless of when someone joined); this gate decides who is allowed
# to be attached, so a stranger can't join a class they're not part of.

def learner_in_course(lp, course_id):
    """A learner may join a course's class-chat iff they hold LIVE access to
    the course. Access is defined by an active, non-expired subscription — the
    SAME rule that gates course content (has_active_subscription) — so chat
    membership never drifts from content access when a subscription lapses
    (patch set 3: previously this checked the Enrollment row, which stayed
    ACTIVE after the subscription expired).

    Falls back to the raw ACTIVE-enrollment check only if the subscription
    helper can't be imported (keeps rooms working during partial deploys).
    """
    try:
        from courses.models import Course
        from enrollments.services import has_active_subscription
        course = Course.objects.filter(id=course_id).first()
        if course is None:
            return False
        return has_active_subscription(
            user=lp.account, course=course, learner_profile=lp
        )
    except Exception:
        try:
            from enrollments.models import Enrollment
            return Enrollment.objects.filter(
                learner_profile=lp, course_id=course_id,
                status=Enrollment.STATUS_ACTIVE,
            ).exists()
        except Exception:
            return False

def teacher_in_course(tp, course_id):
    try:
        from courses.models import SubjectTeacher
        return SubjectTeacher.objects.filter(
            subject__course_id=course_id, teacher=tp.user
        ).exists()
    except Exception:
        return False


def can_join_course_room(kind, obj, course_id):
    if kind == Participant.KIND_LEARNER:
        return learner_in_course(obj, course_id)
    if kind == Participant.KIND_TEACHER:
        return teacher_in_course(obj, course_id)
    return False


def course_room_track(course_id):
    """Best-effort track for a course room, used by the inbox filter tabs.
    Returns "academy", "skilldev", or None (never raises)."""
    if not course_id:
        return None
    try:
        from courses.models import Course
        if Course.objects.filter(id=course_id).exists():
            return "academy"
    except Exception:
        pass
    try:
        from skills.course_models import SkillCourse
        if SkillCourse.objects.filter(id=course_id).exists():
            return "skilldev"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DM policy lookups (Phase 3 §10) — the data chat/policy.py's DM matrix
# rules need. Kept here, not in policy.py, for the same reason course
# membership lives here rather than in policy.py: this module owns queries,
# policy.py owns the yes/no decision built from them.
# ---------------------------------------------------------------------------

def learner_teacher_share_active_course(lp, tp):
    """True iff there's at least one course where `lp` holds live access
    (learner_in_course()) AND `tp` teaches a subject (teacher_in_course())
    — the "if_relationship" rule for a learner starting a DM with a
    teacher. Reuses learner_in_course()/teacher_in_course() rather than
    re-deriving the subscription/enrollment rule a second time; the number
    of courses one teacher teaches is small enough that checking each is
    cheaper than building a second, more clever query.
    """
    try:
        from courses.models import SubjectTeacher
        course_ids = (
            SubjectTeacher.objects.filter(teacher=tp.user)
            .values_list("subject__course_id", flat=True)
            .distinct()
        )
        return any(learner_in_course(lp, cid) for cid in course_ids)
    except Exception:
        logger.exception(
            "chat.services: learner_teacher_share_active_course failed for "
            "lp=%s tp=%s", getattr(lp, "id", None), getattr(tp, "id", None),
        )
        return False


def learners_share_room(lp_a, lp_b):
    """True iff both learners are already LEARNER participants of at least
    one shared kind=ROOM conversation — the "same_room_only" rule for
    learner<->learner DMs. Pure chat-data query (no vertical import
    needed, unlike the function above)."""
    if not lp_a or not lp_b or lp_a.id == lp_b.id:
        return False
    rooms_with_a = Conversation.objects.filter(
        kind=Conversation.KIND_ROOM,
        participants__kind=Participant.KIND_LEARNER,
        participants__learner_profile=lp_a,
    )
    return rooms_with_a.filter(
        participants__kind=Participant.KIND_LEARNER,
        participants__learner_profile=lp_b,
    ).exists()


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
    # M1 dual-write — see _attach_participant()'s identical note on why
    # `defaults` (create-only) is the right place for this.
    defaults["blocker_identity"] = _get_identity(blocker_kind, blocker_obj)
    defaults["blocked_identity"] = _get_identity(blocked_kind, blocked_obj)
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

    # Idempotency: if THIS SENDER already posted this client_id in this
    # room, return that message rather than creating a duplicate. Scoped
    # by sender to match unique_message_client_id_per_sender exactly
    # (chat/models.py) — two different senders coincidentally generating
    # the same client-side id must NOT collide. (Found via this stage's
    # M0 regression pass: the pre-check here was previously unscoped by
    # sender, so it could return a different sender's message entirely on
    # a same-client_id coincidence — a pre-existing bug, not new in M3.)
    if client_id:
        existing = conversation.messages.filter(
            client_id=client_id, sender=sender_participant,
        ).first()
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

    # M3 (Phase 3 §11): SAME transaction as the Message above — the whole
    # point of the outbox pattern. See OutboxEvent's docstring and
    # chat/outbox_handlers.py for what drains this.
    OutboxEvent.objects.create(
        event_type=OutboxEvent.EVENT_MESSAGE_CREATED,
        payload={
            "conversation_id": str(conversation.id),
            "message_id": str(msg.id),
        },
    )

    _fanout_new_message(conversation, sender_participant, msg)
    return msg


def _unread_from_db(conversation, participant):
    """DB-truth unread count. Pre-M0 this ran on every serialize_conversation()
    call; now it's only the fallback for a Redis cache miss (see
    redis_utils.get_unread_count) and the seed for a fresh counter."""
    qs = conversation.messages.exclude(sender=participant)
    if participant.last_read_at:
        qs = qs.filter(created_at__gt=participant.last_read_at)
    return qs.count()


def _fanout_new_message(conversation, sender_participant, msg):
    """M0: bump each OTHER participant's Redis unread counter and push a
    realtime inbox_delta to their account-level ws/updates/ socket, so an
    open inbox re-sorts and re-badges without a refetch — including for a
    participant who is offline from THIS conversation's thread (they still
    get the delta; they just won't see it until they open the app/tab).

    Best-effort by design: every call inside is fail-open (see redis_utils /
    realtime docstrings), so a Redis or Celery outage degrades to "no live
    badge update, counters resync from the DB on next read" — it can never
    undo or fail the message send that already committed above.
    """
    preview = {
        "sender_name": sender_participant.display_name() if sender_participant else "Unknown",
        "body": msg.body[:140],
    }
    others = conversation.participants.exclude(id=getattr(sender_participant, "id", None))
    for p in others:
        redis_utils.increment_unread(p.identity_key(), conversation.id)

        account_id = p.account_id
        if not account_id:
            continue
        unread = redis_utils.get_unread_count(
            p.identity_key(), conversation.id,
            rebuild_fn=lambda p=p: _unread_from_db(conversation, p),
        )
        realtime.push_inbox_delta(account_id, {
            "conversation_id": str(conversation.id),
            "unread": unread,
            "preview": preview,
            "last_message_at": conversation.last_message_at.isoformat(),
            # Matches the {audience, learner_profile_id} keys
            # accounts.consumers.UserUpdateConsumer._wanted() already filters
            # on for notifications — reused as-is, no consumer changes needed
            # beyond adding the inbox_delta handler method.
            "audience": "TEACHER" if p.kind == Participant.KIND_TEACHER else "LEARNER",
            "learner_profile_id": (
                str(p.learner_profile_id) if p.kind == Participant.KIND_LEARNER else None
            ),
        })


def post_message_checked(conversation, sender_participant, body, client_id=""):
    """
    The one gate every message passes through.

    Returns (message, error):
      • (msg, None)                      → saved & should be broadcast
      • (None, {"category","reason"})    → refused (policy, moderation, or
                                            block); DO NOT broadcast — tell
                                            the sender.
      • (None, None)                     → empty body, nothing to do.
    """
    text = (body or "").strip()
    if not text:
        return None, None

    # 0) structural policy gate (Phase 3 §10) — frozen conversation,
    # read-only broadcast. Runs BEFORE moderation: a message into a closed
    # or read-only conversation is refused regardless of its content.
    allowed, reason = policy.can_post(conversation, sender_participant)
    if not allowed:
        return None, {"category": "policy", "reason": reason}

    # 1) content moderation (profanity + political/controversial)
    verdict = moderation.check_message(text)
    if not verdict.ok:
        return None, {"category": verdict.category, "reason": verdict.reason}

    # 2) blocking — only meaningful on a 1:1 thread (a group room has no
    # single counterpart to be blocked by)
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
    if me_participant:
        unread = redis_utils.get_unread_count(
            me_participant.identity_key(), conv.id,
            rebuild_fn=lambda: _unread_from_db(conv, me_participant),
        )

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

    is_course_room = conv.kind == Conversation.KIND_ROOM and conv.context_type == "course"
    track = course_room_track(conv.course_id) if is_course_room else None

    return {
        "id": str(conv.id),
        "kind": conv.kind,
        "track": track,
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
