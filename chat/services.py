# PLACEMENT: backend/backend/chat/services.py   (FULL FILE — REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/services.py
#
# M3 changes (Phase 3 §9/§10/§11), on top of everything below:
#   • ensure_course_room() is now a thin wrapper over the new, generalized
#     ensure_room(context_type, context_id, title). Same signature/behaviour
#     for its one existing caller (chat/views.py's CourseRoomView).
#   • New lookups backing chat/policy.py's DM matrix: teacher_is_public_faculty(),
#     learner_teacher_share_active_course(), and learners_share_room().
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
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
import logging

from accounts.models import LearnerProfile, TeacherProfile, Identity
from .models import (
    Conversation, Participant, Message, Block, OutboxEvent,
    MessageAttachment, MessageReaction, Report, ChatSuspension,
    CommPreference, SupportTicket,
)
from . import attachments as attachment_rules
from . import moderation
from . import redis_utils
from . import realtime
from . import policy

logger = logging.getLogger(__name__)

# Stage B (CC-007/010/015): the fixed, safe reaction set. A plain CharField
# on MessageReaction.emoji has no DB-level constraint on purpose (see that
# model's docstring) — this is the one place that decides what's actually
# reachable, so widening the palette later is a one-line change, not a
# migration.
ALLOWED_REACTIONS = {"👍", "❤️", "😂", "😮", "😢", "🙏"}


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
    if kind == Participant.KIND_LEARNER:
        return f"L:{obj_id}"
    if kind == Participant.KIND_TEACHER:
        return f"T:{obj_id}"
    return f"S:{obj_id}"


def resolve_identity(kind, obj_id):
    """Fetch the LearnerProfile / TeacherProfile / staff User for a
    (kind, id) pair, or None."""
    if kind == Participant.KIND_TEACHER:
        return TeacherProfile.objects.filter(id=obj_id).first()
    if kind == Participant.KIND_LEARNER:
        return LearnerProfile.objects.filter(id=obj_id, is_active=True).first()
    if kind == Participant.KIND_STAFF:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.filter(id=obj_id, is_staff=True).first()
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


@transaction.atomic
def ensure_broadcast_room(context_type, context_id, title=""):
    """Get or create the unique BROADCAST (read-only-for-non-teachers)
    conversation for (context_type, context_id) — the Announcements channel
    for e.g. a course (Stage D · CC-015). Sibling of ensure_room() above;
    kept as its OWN function rather than a `kind=` parameter on ensure_room()
    so that function's existing "behaviour is unchanged for its one caller"
    guarantee stays literally true, and so the two kinds can never collide
    on Conversation's `unique_room_per_context` / `unique_broadcast_per_context`
    constraints (one context can legitimately have both a ROOM and a
    BROADCAST — Discussion and Announcements are different channels)."""
    conv, created = Conversation.objects.get_or_create(
        context_type=context_type,
        context_id=context_id,
        kind=Conversation.KIND_BROADCAST,
        defaults={"title": title},
    )
    if not created and title and conv.title != title:
        conv.title = title
        conv.save(update_fields=["title"])
    return conv


def ensure_course_announcements(course_id, title=""):
    """Thin wrapper mirroring ensure_course_room() — the one call site
    (chat/views.py's CourseAnnouncementsView) always has a course id, never
    a raw context_id."""
    return ensure_broadcast_room("course", str(course_id), title or "Announcements")


def participant_for(conversation, kind, obj):
    """Return the Participant row for this identity in a conversation, or None."""
    qs = conversation.participants.filter(kind=kind)
    if kind == Participant.KIND_LEARNER:
        return qs.filter(learner_profile=obj).first()
    if kind == Participant.KIND_STAFF:
        return qs.filter(staff_user=obj).first()
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
    if participant.kind == Participant.KIND_STAFF:
        return ["staff"]
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

    `course_id` can come from either the academy `courses.Course` table or
    the `skills.SkillCourse` marketplace table — same convention already
    used by course_room_track()/_course_badge() below for display: try
    Course first, and only fall through to SkillCourse if that lookup finds
    nothing. Both use random UUID primary keys with no shared ID space, so
    this is the same negligible-collision assumption already shipped there.
    """
    try:
        from courses.models import Course
        from enrollments.services import has_active_subscription
        course = Course.objects.filter(id=course_id).first()
        if course is not None:
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

    try:
        from skills.course_models import SkillCourseEnrollment
        return SkillCourseEnrollment.objects.filter(
            learner_profile=lp, course_id=course_id,
            status=SkillCourseEnrollment.STATUS_ACTIVE,
        ).exists()
    except Exception:
        return False

def teacher_in_course(tp, course_id):
    try:
        from courses.models import TeachingAssignment
        if TeachingAssignment.objects.filter(
            subject__course_id=course_id, teacher=tp.user, is_active=True,
        ).exists():
            return True
    except Exception:
        pass

    try:
        from skills.course_models import SkillCourse
        return SkillCourse.objects.filter(id=course_id, teacher_profile=tp).exists()
    except Exception:
        return False


def can_join_course_room(kind, obj, course_id):
    if kind == Participant.KIND_LEARNER:
        return learner_in_course(obj, course_id)
    if kind == Participant.KIND_TEACHER:
        return teacher_in_course(obj, course_id)
    return False


def is_course_membership_still_valid(conversation, participant):
    """A Participant row is only ever created once, at join time, and never
    revoked when the underlying Enrollment/TeachingAssignment ends — so its
    mere existence isn't proof of current access. Re-derives access from the
    live DB relationship for course-context ROOM/BROADCAST conversations
    (the only kind with a revocable backing relationship to re-check here).
    Everything else (DIRECT, SESSION, SUPPORT, counseling, etc.) has no
    such relationship yet, so this returns True for those unconditionally.
    """
    if conversation.context_type != "course" or not conversation.context_id:
        return True
    obj = (
        participant.learner_profile
        if participant.kind == Participant.KIND_LEARNER
        else participant.teacher_profile
    )
    if obj is None:
        return False
    return can_join_course_room(participant.kind, obj, conversation.context_id)


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

def teacher_is_public_faculty(tp):
    """True iff `tp` is a publicly reachable teacher a learner may start a DM
    with — one of the ways to satisfy the "if_relationship" rule (see
    learner_teacher_share_active_course() for the other).

    "Publicly reachable" = approved on EITHER track (academy_status or
    skill_status == TRACK_APPROVED), or a listed guest expert
    (ExpertProfile.is_listed).

    THIS IS INTENTIONALLY WIDER THAN THE ACADEMY TEACHER DIRECTORY. Do not
    "align" it by narrowing to academy_status — that is backwards, and it was
    tried once already. Being messageable and being bookable-as-faculty are
    different questions:
      • accounts.TeacherListView (Academy Teachers page + private-session
        form) is academy-only, because a guest expert there is unvetted and
        the booking 400s anyway.
      • Skill Dev surfaces its own experts via Explore Experts, and every one
        of them offers a Message button. Gating DMs on academy approval 403'd
        exactly those experts — and, since a teacher DMing a learner runs
        through this check with themselves as `tp`, it also left them unable
        to message anyone from their own Skill Dev inbox.
    Mirrors _teacher_roles()'s own guest detection (skill_status ==
    TRACK_APPROVED), so the DM gate and the role labels can't drift.

    Approval alone is deliberately sufficient, with no shared-course/
    enrollment check: the directory already lists every approved/listed
    teacher regardless of subject assignment (that's the whole point of
    "Explore Experts"), so requiring a shared course on top would 403 exactly
    the "Message" button the directory just offered. It also means this scales
    as the expert pool grows, without needing a subject-teacher assignment for
    every learner/teacher pair up front.
    """
    try:
        ep = getattr(tp, "expert_profile", None)
        if ep and ep.is_listed:
            return True
    except Exception:
        pass
    try:
        if tp.academy_status == TeacherProfile.TRACK_APPROVED:
            return True
    except Exception:
        pass
    try:
        if tp.skill_status == TeacherProfile.TRACK_APPROVED:
            return True
    except Exception:
        pass
    return False


def learner_teacher_share_active_course(lp, tp):
    """True iff there's at least one course where `lp` holds live access
    (learner_in_course()) AND `tp` teaches a subject (teacher_in_course())
    — the other way to satisfy the "if_relationship" rule (see
    teacher_is_public_faculty() for the directory-membership way). Kept as
    an independent OR so a teacher assigned to a learner's course can still
    be messaged even in the (data-inconsistent but possible) case they
    aren't yet approved/listed. Reuses learner_in_course()/
    teacher_in_course() rather than re-deriving the subscription/enrollment
    rule a second time; the number of courses one teacher teaches is small
    enough that checking each is cheaper than building a second, more
    clever query.
    """
    try:
        from courses.models import TeachingAssignment
        course_ids = (
            TeachingAssignment.objects.filter(teacher=tp.user, is_active=True)
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
def post_message(conversation, sender_participant, body, client_id="", reply_to=None, message_type=None):
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
            # Re-broadcast rather than silently returning: on the ORIGINAL
            # send, _finalize_new_message() below is what actually pushes
            # the "chat.message" frame the sender's own client is waiting
            # on to clear its ack timer. Skipping straight past that here
            # (as this used to) meant a resent client_id — the exact retry
            # path a dropped ack triggers — got no frame and no error,
            # ever: the bubble stays "failed" forever no matter how many
            # times the client retries, even though the message was
            # delivered on the first try. Re-broadcasting is safe (not a
            # duplicate to anyone): every connected client, including the
            # original sender, already dedupes an incoming "message" frame
            # by client_id/id (see ConversationThread.jsx's onMessage).
            # Deliberately NOT calling _finalize_new_message() again — it
            # also writes a fresh OutboxEvent and re-bumps unread/inbox_delta
            # for every other participant, which would double-notify them.
            realtime.push_conversation_event(
                conversation.id, "chat.message", serialize_message(existing),
            )
            return existing

    if message_type is None:
        # Stage D (CC-015): a message posted into a BROADCAST room is an
        # Announcement by definition — tagging it here means the frontend
        # can style/label it without a second fetch of the parent
        # conversation just to learn its kind.
        message_type = (
            Message.TYPE_ANNOUNCEMENT if conversation.kind == Conversation.KIND_BROADCAST
            else Message.TYPE_TEXT
        )

    msg = Message.objects.create(
        conversation=conversation,
        sender=sender_participant,
        body=body[:4000],
        client_id=client_id,
        reply_to=reply_to,
        message_type=message_type,
    )
    _finalize_new_message(conversation, sender_participant, msg)
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


def _finalize_new_message(conversation, sender_participant, msg):
    """The common tail of every new-message path — post_message() above for
    a plain text send, _create_attachment_message() below for an attachment.
    Stamps last_message_at, writes the OutboxEvent (Phase 3 §11 — the
    offline notification pipeline; chat/outbox_handlers.py drains this and
    decides email/SMS/push per notifications/policy.py), runs the M0
    per-participant unread bump + inbox_delta (_fanout_new_message() above),
    and — Stage C addition — broadcasts the message itself to the
    conversation's own live group.

    That last part used to be consumers.py's job, done only for a
    WS-originated send. Centralizing it here means an attachment uploaded
    over REST fans out to every open thread exactly like a typed message
    does, with no special case anywhere else — see consumers.py's receive()
    "message" branch, which no longer group_sends on success itself, and
    chat/views.py's ConversationAttachmentUploadView, which never has to
    remember to either.
    """
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at"])

    OutboxEvent.objects.create(
        event_type=OutboxEvent.EVENT_MESSAGE_CREATED,
        payload={
            "conversation_id": str(conversation.id),
            "message_id": str(msg.id),
        },
    )

    _fanout_new_message(conversation, sender_participant, msg)
    realtime.push_conversation_event(conversation.id, "chat.message", serialize_message(msg))


SUSPENDED_REASON = (
    "Your ability to send messages has been temporarily restricted by a "
    "platform moderator."
)


def is_suspended(participant):
    """Stage D (CC-023): true iff this identity currently has an active
    ChatSuspension. Deliberately a plain DB read with no try/except —
    unlike the Redis helpers in this module (which fail OPEN on purpose;
    see redis_utils.py's module docstring), a query failure here should
    surface as a 500, not silently let a suspended sender's message
    through."""
    suspension = ChatSuspension.objects.filter(identity_key=participant.identity_key()).first()
    return bool(suspension and suspension.is_active())


def post_message_checked(conversation, sender_participant, body, client_id="", reply_to_id=None):
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

    # -1) suspension gate (Stage D · CC-023) — checked first, so a
    # suspended sender is refused identically no matter which conversation
    # or endpoint (WS send, REST attachment, ticket reply) they try.
    if is_suspended(sender_participant):
        return None, {"category": "suspended", "reason": SUSPENDED_REASON}

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

    # 3) reply target (Stage B · CC-007/010) — best-effort: an invalid,
    # already-deleted, or missing reply_to_id is silently dropped rather
    # than refusing the whole send.
    reply_to = None
    if reply_to_id:
        reply_to = conversation.messages.filter(id=reply_to_id, deleted_at__isnull=True).first()

    msg = post_message(conversation, sender_participant, text, client_id, reply_to=reply_to)
    return msg, None


def post_attachment_checked(conversation, sender_participant, django_file, caption="", reply_to_id=None):
    """Attachment counterpart to post_message_checked() (Stage C · CC-012).
    Same gate ORDER (suspension → structural policy → moderation-of-the-
    caption → block → CC-012's own size/type validation), and the same
    (message, error) return shape, so chat/views.py's
    ConversationAttachmentUploadView handles a rejection exactly like every
    other rejected send — no attachment-specific error branch needed on
    the frontend beyond reading `category`."""
    if is_suspended(sender_participant):
        return None, {"category": "suspended", "reason": SUSPENDED_REASON}

    allowed, reason = policy.can_post(conversation, sender_participant)
    if not allowed:
        return None, {"category": "policy", "reason": reason}

    caption = (caption or "").strip()
    if caption:
        verdict = moderation.check_message(caption)
        if not verdict.ok:
            return None, {"category": verdict.category, "reason": verdict.reason}

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

    meta, err = attachment_rules.classify_and_validate(django_file)
    if err:
        return None, {"category": "attachment", "reason": err}

    reply_to = None
    if reply_to_id:
        reply_to = conversation.messages.filter(id=reply_to_id, deleted_at__isnull=True).first()

    msg = _create_attachment_message(conversation, sender_participant, django_file, meta, caption, reply_to)
    return msg, None


@transaction.atomic
def _create_attachment_message(conversation, sender_participant, django_file, meta, caption, reply_to):
    message_type = Message.TYPE_IMAGE if meta["kind"] == MessageAttachment.KIND_IMAGE else Message.TYPE_FILE
    msg = Message.objects.create(
        conversation=conversation, sender=sender_participant, body=caption[:4000],
        reply_to=reply_to, message_type=message_type,
    )
    from django.conf import settings
    MessageAttachment.objects.create(
        conversation=conversation, message=msg, uploaded_by=sender_participant,
        file=django_file, kind=meta["kind"], original_name=meta["name"],
        content_type=meta["content_type"], size_bytes=meta["size"],
        expires_at=timezone.now() + timedelta(days=settings.CHAT_ATTACHMENT_EXPIRY_DAYS),
    )
    _finalize_new_message(conversation, sender_participant, msg)
    return msg


# ---------------------------------------------------------------------------
# Reactions  (Stage B · CC-007/010/015)
# ---------------------------------------------------------------------------

def toggle_reaction(msg, participant, emoji):
    """Add or remove one (message, participant, emoji) row — sending the
    same emoji twice is a toggle-off, not an error (see MessageReaction's
    docstring). Returns (action, summary): action is "added"/"removed";
    summary is the same list-of-{emoji,count,identities} shape
    serialize_message() embeds, so the REST response and the realtime
    broadcast payload can share one code path."""
    existing = MessageReaction.objects.filter(message=msg, participant=participant, emoji=emoji).first()
    if existing:
        existing.delete()
        action = "removed"
    else:
        MessageReaction.objects.create(message=msg, participant=participant, emoji=emoji)
        action = "added"
    return action, _reaction_summary(msg)


def _reaction_summary(msg):
    out = {}
    for r in msg.reactions.select_related("participant").all():
        entry = out.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "identities": []})
        entry["count"] += 1
        entry["identities"].append(r.participant.identity_key() if r.participant else None)
    return list(out.values())


# ---------------------------------------------------------------------------
# Soft delete  (Stage B · CC-006/010)
# ---------------------------------------------------------------------------

def can_delete_message(participant, msg):
    """Who may soft-delete a message:
      • the sender, always;
      • inside a ROOM only, a TEACHER participant (in-class moderation —
        e.g. removing an off-topic message from a course's Discussion tab).
    Admin/moderator removal is a SEPARATE path — soft_delete_message()
    called with admin_reason=... from an IsAdmin-gated view — that never
    calls this check at all; see that function's docstring for why."""
    if msg.deleted_at:
        return False
    if msg.sender_id == participant.id:
        return True
    if msg.conversation.kind == Conversation.KIND_ROOM and participant.kind == Participant.KIND_TEACHER:
        return True
    return False


def soft_delete_message(msg, participant=None, admin_reason=""):
    """`participant` set (admin_reason blank) → an ordinary self/in-room-
    teacher delete; the tombstone reads "Message deleted". `admin_reason`
    set (participant left None) → a moderator removal via the Reports queue
    or AdminRemoveMessageView; the tombstone reads "Removed by a
    moderator". The two are mutually exclusive by convention, not by a DB
    constraint — every call site in this codebase only ever sets one."""
    msg.deleted_at = timezone.now()
    msg.deleted_by = participant
    msg.deleted_reason = admin_reason
    msg.save(update_fields=["deleted_at", "deleted_by", "deleted_reason"])

    # Purge the file from storage on every deletion path — self-delete,
    # moderator removal, and the daily expiry sweep all route through
    # here. There's no reuse of the bytes elsewhere (MessageAttachment is a
    # strict OneToOne on Message), so there's no correctness reason to keep
    # them around once the message is gone; leaving them meant "temporary
    # file sharing" and moderator removal were both cosmetic — the file
    # stayed downloadable by anyone who'd kept the URL. Best-effort: a
    # missing/already-gone blob must not block the tombstone above, which
    # has already been committed.
    try:
        msg.attachment.file.delete(save=False)
    except MessageAttachment.DoesNotExist:
        pass
    except Exception:
        logger.exception(
            "chat.services: failed to delete attachment file for message %s", msg.id,
        )


# ---------------------------------------------------------------------------
# Chat-level suspension  (Stage D · CC-023)
# ---------------------------------------------------------------------------

def suspend_identity(identity_key, reason, created_by, until=None):
    suspended_until = None
    if until:
        suspended_until = until if hasattr(until, "tzinfo") else parse_datetime(str(until))
    obj, _ = ChatSuspension.objects.update_or_create(
        identity_key=identity_key,
        defaults={
            "reason": (reason or "")[:255],
            "created_by": created_by,
            "suspended_until": suspended_until,
        },
    )
    return obj


def lift_suspension(identity_key):
    return ChatSuspension.objects.filter(identity_key=identity_key).delete()


# ---------------------------------------------------------------------------
# Staff participants  (Stage D · CC-022 support tickets)
# ---------------------------------------------------------------------------

def _get_staff_identity(user):
    """Best-effort accounts.Identity row for a staff/admin user — reuses the
    KIND_SYSTEM letter that model already reserves for exactly this
    ("announcement / support bot senders"). Never raises: a missing
    Identity row just leaves the Participant's `identity` dual-write FK
    unset; `staff_user` remains the source of truth either way, the same
    fallback shape _get_identity() already uses for learner/teacher rows."""
    try:
        identity, _ = Identity.objects.get_or_create(
            kind=Identity.KIND_SYSTEM,
            profile_id=str(user.id),
            defaults={
                "display_name": user.get_full_name() or user.username or "Support",
                "account": user,
            },
        )
        return identity
    except Exception:
        logger.exception(
            "chat.services: staff identity lookup/create failed for user=%s",
            getattr(user, "id", None),
        )
        return None


def attach_staff_participant(conversation, user):
    """Get-or-create the STAFF Participant for `user` in `conversation` —
    used only for SUPPORT tickets (see chat/views.py's
    SupportTicketReplyView / _ticket_and_participant()). Deliberately NOT
    used for ad-hoc admin actions on an arbitrary DIRECT/ROOM conversation
    (e.g. removing a reported message) — see soft_delete_message()'s
    docstring for why an admin acting on a conversation they aren't part of
    should never become a Participant of it."""
    identity = _get_staff_identity(user)
    return Participant.objects.get_or_create(
        conversation=conversation, kind=Participant.KIND_STAFF, staff_user=user,
        defaults={"identity": identity},
    )[0]


# ---------------------------------------------------------------------------
# Academic support tickets  (Stage D · CC-022)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_support_ticket(kind, obj, subject, category, body):
    """Returns (ticket, error) — error uses the same {"category","reason"}
    shape as every other rejected send, checked BEFORE any row exists so a
    moderation-rejected first message never leaves behind an empty ticket."""
    text = (body or "").strip()
    if not text:
        return None, {"category": "empty", "reason": "Describe your issue before submitting."}
    verdict = moderation.check_message(text)
    if not verdict.ok:
        return None, {"category": verdict.category, "reason": verdict.reason}

    conv = Conversation.objects.create(kind=Conversation.KIND_SUPPORT, title=subject[:200])
    requester = _attach_participant(conv, kind, obj)
    ticket = SupportTicket.objects.create(
        conversation=conv,
        requester_kind=kind,
        requester_learner=obj if kind == Participant.KIND_LEARNER else None,
        requester_teacher=obj if kind == Participant.KIND_TEACHER else None,
        subject=subject[:200],
        category=category,
    )
    post_message(conv, requester, text)
    return ticket, None


def serialize_ticket(ticket):
    last = (
        ticket.conversation.messages.filter(deleted_at__isnull=True)
        .order_by("-created_at").select_related("sender").first()
    )
    if ticket.requester_kind == Participant.KIND_LEARNER and ticket.requester_learner:
        requester_name = ticket.requester_learner.display_name
    elif ticket.requester_kind == Participant.KIND_TEACHER and ticket.requester_teacher:
        requester_name = teacher_display_name(ticket.requester_teacher)
    else:
        requester_name = "Unknown"
    requester_identity = identity_key_for_ids(
        ticket.requester_kind,
        ticket.requester_learner_id if ticket.requester_kind == Participant.KIND_LEARNER else ticket.requester_teacher_id,
    )
    return {
        "id": str(ticket.id),
        "conversation_id": str(ticket.conversation_id),
        "subject": ticket.subject,
        "category": ticket.category,
        "status": ticket.status,
        "requester_name": requester_name,
        # Lets a requester-side UI (student/teacher SupportView) render
        # "mine" vs "staff" without needing its own viewer-identity plumbed
        # through — every message NOT from this identity is a staff reply.
        "requester_identity": requester_identity,
        "assignee": (
            (ticket.assignee.get_full_name() or ticket.assignee.username)
            if ticket.assignee else None
        ),
        "assignee_id": str(ticket.assignee_id) if ticket.assignee_id else None,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
        "last_message": serialize_message(last) if last else None,
    }


# ---------------------------------------------------------------------------
# Admin platform broadcast  (Stage D · CC-023)
# ---------------------------------------------------------------------------

def send_admin_broadcast(audience, title, body, link_url, actor):
    """A platform-wide blast is a pure notifications.services.notify_many()
    fanout, NOT a Conversation — unlike the per-course Announcements feature
    (ensure_course_announcements() above), "all students" has no single
    context to hang a BROADCAST room off, and creating one Participant row
    per recipient for a one-off announcement would be a lot of write
    amplification for something nobody replies within anyway. Returns the
    recipient count."""
    from django.contrib.auth import get_user_model
    from notifications.services import notify_many

    User = get_user_model()
    qs = User.objects.filter(is_active=True)
    if audience == "all_students":
        qs = qs.filter(learner_profiles__isnull=False).distinct()
    elif audience == "all_teachers":
        qs = qs.filter(teacher_profile__isnull=False)
    # audience == "all" (or anything unrecognized) → every active user.

    users = list(qs)
    notify_many(
        users,
        verb="announcement.posted",
        title=title,
        body=body,
        actor=actor,
        link_url=link_url or "",
    )
    return len(users)


# ---------------------------------------------------------------------------
# Course Hub composition  (Stage C · CC-013/014/016)
# ---------------------------------------------------------------------------
# The data for Resources and Assignments already exists (materials.StudyMaterial,
# assignments.Assignment) — this is purely a read-side composition over it, the
# same lazy-import-and-never-raise discipline learner_in_course() etc. above
# already use for cross-app lookups.

def course_resources(course_id):
    try:
        from materials.models import StudyMaterial
        items = (
            StudyMaterial.objects
            .filter(chapter__subject__course_id=course_id)
            .select_related("chapter")
            .prefetch_related("files")
            .order_by("-created_at")[:100]
        )
        out = []
        for m in items:
            files = [
                {"id": str(f.id), "name": f.filename(), "url": (f.file.url if f.file else None)}
                for f in m.files.all()
            ]
            out.append({
                "id": str(m.id),
                "title": m.title,
                "chapter": m.chapter.title,
                "created_at": m.created_at.isoformat(),
                "files": files,
            })
        return out
    except Exception:
        logger.exception("chat.services: course_resources failed for course_id=%s", course_id)
        return []


def course_assignments_summary(course_id):
    try:
        from assignments.models import Assignment
        items = (
            Assignment.objects
            .filter(chapter__subject__course_id=course_id)
            .select_related("chapter")
            .prefetch_related("files")
            .order_by("-due_date")[:100]
        )
        out = []
        for a in items:
            files = [
                {"id": str(f.id), "name": f.original_filename or "file",
                 "url": (f.file.url if f.file else None)}
                for f in a.files.all()
            ]
            out.append({
                "id": str(a.id),
                "title": a.title,
                "chapter": a.chapter.title,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "is_expired": a.is_expired,
                "files": files,
            })
        return out
    except Exception:
        logger.exception("chat.services: course_assignments_summary failed for course_id=%s", course_id)
        return []


def _course_badge(course_id):
    """Best-effort {id, title, board_name} for a ROOM/BROADCAST's course context
    — checks both the academy `courses` app and the `skills` marketplace,
    since a course room's context_id can come from either (course_room_track()
    above does the identical double-check for the same reason).

    `board_name` is here because this one dict is the ONLY class indicator on a
    chat row: it feeds the conversation-list badge, the thread subtitle, the
    Course Hub header and the announcements course picker, on both dashboards.
    Course titles no longer carry the board, so without it a teacher picking a
    course to announce to sees two identical "Class 9" chips. Always None for
    SkillCourse — the marketplace has no boards — so consumers must treat it as
    optional rather than assuming an academy course. See
    courses/board_display.py for why the field is a flat nullable string."""
    if not course_id:
        return None
    try:
        from courses.models import Course
        from courses.board_display import board_name_for
        c = (
            Course.objects
            .filter(id=course_id)
            .select_related("board")          # else one extra query per row
            .only("id", "title", "board__name")
            .first()
        )
        if c:
            return {"id": str(c.id), "title": c.title, "board_name": board_name_for(c)}
    except Exception:
        pass
    try:
        from skills.course_models import SkillCourse
        c = SkillCourse.objects.filter(id=course_id).first()
        if c:
            return {"id": str(c.id), "title": getattr(c, "title", "") or "Course",
                    "board_name": None}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Directory  (people you can start a chat with — CC-002/018)
# ---------------------------------------------------------------------------

def teacher_display_name(tp):
    """Mirror ExpertProfile/Participant naming so the directory matches the
    rest of the app: prefer the teacher's SELF learner-profile name, then
    username/email. Never raises."""
    try:
        u = tp.user
        lp = u.self_learner_profile()
        if lp:
            name = f"{(lp.first_name or '').strip()} {(lp.last_name or '').strip()}".strip()
            if name:
                return name
            if getattr(lp, "full_name", ""):
                return lp.full_name
            if getattr(lp, "display_name", ""):
                return lp.display_name
        return u.username or u.email
    except Exception:
        return "Teacher"


def role_label(roles):
    if "faculty" in roles and "guest" in roles:
        return "Faculty · Guest expert"
    if "guest" in roles:
        return "Guest expert"
    if "faculty" in roles:
        return "Faculty"
    if "staff" in roles:
        return "Support Team"
    return "Teacher"


def directory_entries(kind, obj, q=""):
    """The "start a new chat" people directory — listed guest experts +
    approved faculty teachers, deduped and role-merged. Factored out of
    chat/views.py's DirectoryView (unchanged behaviour) so
    GlobalSearchView's "people" results can share it instead of
    reimplementing the same ~60 lines."""
    q = (q or "").strip().lower()
    entries = {}

    try:
        from skills.models import ExpertProfile
        experts = (
            ExpertProfile.objects
            .filter(is_listed=True)
            .select_related("teacher_profile", "teacher_profile__user")
        )
        for ep in experts:
            tp = ep.teacher_profile
            if not tp:
                continue
            roles = _teacher_roles(tp)
            avatar = None
            try:
                if ep.photo:
                    avatar = ep.photo.url
                elif tp.photo:
                    avatar = tp.photo.url
            except Exception:
                avatar = None
            entries[str(tp.id)] = {
                "target_kind": Participant.KIND_TEACHER,
                "target_id": str(tp.id),
                "name": teacher_display_name(tp),
                "roles": roles,
                "role_label": role_label(roles),
                "subtitle": (ep.headline or role_label(roles)),
                "avatar": avatar,
            }
    except Exception:
        pass

    try:
        faculty = (
            TeacherProfile.objects
            .filter(academy_status=TeacherProfile.TRACK_APPROVED)
            .select_related("user")
        )
        for tp in faculty:
            key = str(tp.id)
            if key in entries:
                if "faculty" not in entries[key]["roles"]:
                    entries[key]["roles"].append("faculty")
                    entries[key]["role_label"] = role_label(entries[key]["roles"])
                continue
            roles = _teacher_roles(tp)
            avatar = None
            try:
                if tp.photo:
                    avatar = tp.photo.url
            except Exception:
                avatar = None
            entries[key] = {
                "target_kind": Participant.KIND_TEACHER,
                "target_id": key,
                "name": teacher_display_name(tp),
                "roles": roles,
                "role_label": role_label(roles),
                "subtitle": role_label(roles),
                "avatar": avatar,
            }
    except Exception:
        pass

    if kind == Participant.KIND_TEACHER:
        entries.pop(str(getattr(obj, "id", "")), None)

    out = list(entries.values())
    if q:
        out = [e for e in out if q in e["name"].lower() or q in (e["subtitle"] or "").lower()]
    out.sort(key=lambda e: e["name"].lower())
    return out


def build_profile(kind, obj_id):
    """CC-019 User Profile. Deliberately asymmetric with privacy in mind,
    matching directory_entries()'s existing stance ("we only surface the
    public-facing teacher identities"): a TEACHER profile is rich (bio,
    headline, photo, the courses/skills they teach) since that's already
    public-facing marketing material elsewhere in the product; a LEARNER
    profile is name/avatar/roles only — a classmate should be identifiable
    in a room, not browsable. Returns None if the target doesn't exist.
    """
    if kind == Participant.KIND_TEACHER:
        tp = TeacherProfile.objects.filter(id=obj_id).select_related("user").first()
        if not tp:
            return None
        roles = _teacher_roles(tp)
        bio, headline, photo = "", "", None
        try:
            if tp.photo:
                photo = tp.photo.url
        except Exception:
            pass
        try:
            ep = getattr(tp, "expert_profile", None)
            if ep:
                headline = ep.headline or headline
                bio = ep.bio or bio
                if not photo and ep.photo:
                    photo = ep.photo.url
        except Exception:
            pass
        if not bio:
            bio = getattr(tp, "bio", "") or ""
        courses = []
        try:
            from courses.models import TeachingAssignment
            # Title + board, joined for display. Two boards run a course titled
            # "Class 9" each, so a bare title list showed this teacher teaching
            # "Class 9, Class 9". Kept as flat strings because the only consumer
            # (ProfileView's course chips) renders them verbatim.
            rows = (
                TeachingAssignment.objects.filter(teacher=tp.user, is_active=True)
                .select_related("subject", "subject__course", "subject__course__board")
                .values_list("subject__course__title", "subject__course__board__name")
                .distinct()
            )
            seen = set()
            for title, board in rows:
                label = f"{title} · {board}" if board else (title or "")
                if label and label not in seen:
                    seen.add(label)
                    courses.append(label)
        except Exception:
            pass
        return {
            "kind": "TEACHER",
            "id": str(tp.id),
            "name": teacher_display_name(tp),
            "avatar": photo,
            "roles": roles,
            "role_label": role_label(roles),
            "headline": headline,
            "bio": bio,
            "courses": [c for c in courses if c],
        }
    if kind == Participant.KIND_LEARNER:
        lp = LearnerProfile.objects.filter(id=obj_id, is_active=True).first()
        if not lp:
            return None
        return {
            "kind": "LEARNER", "id": str(lp.id), "name": lp.display_name,
            "avatar": lp.avatar_value(), "roles": _learner_roles(lp),
            "role_label": "Student", "headline": "", "bio": "", "courses": [],
        }
    return None


def serialize_report(report):
    return {
        "id": str(report.id),
        "conversation_id": str(report.conversation_id),
        "message_id": str(report.message_id) if report.message_id else None,
        "message_preview": (report.message.body[:200] if (report.message and not report.message.deleted_at) else None),
        "reporter_name": (report.reporter.display_name() if report.reporter else "Unknown"),
        "target_identity": report.target_identity,
        "reason": report.reason,
        "detail": report.detail,
        "status": report.status,
        "resolution_note": report.resolution_note,
        "resolved_by": (
            (report.resolved_by.get_full_name() or report.resolved_by.username)
            if report.resolved_by else None
        ),
        "created_at": report.created_at.isoformat(),
        "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
    }


def _participant_obj_id(p):
    if p.kind == Participant.KIND_LEARNER:
        return p.learner_profile_id
    if p.kind == Participant.KIND_TEACHER:
        return p.teacher_profile_id
    return p.staff_user_id


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_message(msg):
    deleted = msg.deleted_at is not None
    data = {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "body": "" if deleted else msg.body,
        "message_type": msg.message_type,
        "client_id": msg.client_id,
        "created_at": msg.created_at.isoformat(),
        "deleted": deleted,
        "deleted_reason": msg.deleted_reason if deleted else "",
        "sender": {
            "id": str(msg.sender_id) if msg.sender_id else None,
            "name": msg.sender.display_name() if msg.sender else "Unknown",
            "avatar": msg.sender.avatar() if msg.sender else None,
            "identity": msg.sender.identity_key() if msg.sender else None,
        },
    }
    if deleted:
        # A deleted message is a tombstone only — no reply preview,
        # attachment, or reactions leak through it (CC-006's own note on
        # "delete" vs "hide" applies at the message level too: the row
        # survives for moderation history, but nothing it carried renders).
        data["reply_to"] = None
        data["attachment"] = None
        data["reactions"] = []
        return data

    if msg.reply_to_id:
        rt = msg.reply_to
        data["reply_to"] = None if rt is None else {
            "id": str(rt.id),
            "body_preview": ("Message deleted" if rt.deleted_at else rt.body[:140]),
            "sender_name": rt.sender.display_name() if rt.sender else "Unknown",
        }
    else:
        data["reply_to"] = None

    # Reverse OneToOne — Django's RelatedObjectDoesNotExist subclasses
    # AttributeError specifically so getattr(..., None) works here without
    # a try/except.
    attachment = getattr(msg, "attachment", None)
    data["attachment"] = None if attachment is None else {
        "id": str(attachment.id),
        "url": attachment.file.url if attachment.file else None,
        "name": attachment.filename(),
        "size": attachment.size_bytes,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "expires_at": attachment.expires_at.isoformat() if attachment.expires_at else None,
    }

    data["reactions"] = _reaction_summary(msg) if msg.pk else []
    return data


def _participant_dict(p, include_presence=False, include_last_read=False):
    d = {
        "id": str(p.id),
        "name": p.display_name(),
        "avatar": p.avatar(),
        "identity": p.identity_key(),
        "kind": p.kind,
        "roles": participant_roles(p),
    }
    if include_presence or include_last_read:
        pref = CommPreference.for_identity(p.identity_key())
        if include_presence:
            if pref.show_online_status:
                d["online"] = redis_utils.is_online(p.identity_key())
                d["last_seen"] = redis_utils.get_last_seen(p.identity_key())
            else:
                d["online"] = None
                d["last_seen"] = None
        if include_last_read:
            d["last_read_at"] = (
                p.last_read_at.isoformat()
                if (pref.show_read_receipts and p.last_read_at) else None
            )
    return d


def serialize_conversation(conv, me_participant=None):
    parts = list(
        conv.participants
        .select_related("learner_profile", "teacher_profile", "staff_user")
        .all()
    )
    others = [p for p in parts if not (me_participant and p.id == me_participant.id)]

    unread = 0
    if me_participant:
        unread = redis_utils.get_unread_count(
            me_participant.identity_key(), conv.id,
            rebuild_fn=lambda: _unread_from_db(conv, me_participant),
        )

    last = (
        conv.messages.filter(deleted_at__isnull=True)
        .order_by("-created_at")
        .select_related("sender", "reply_to", "reply_to__sender", "attachment")
        .first()
    )

    # Counterpart + blocking state + category all key off the DIRECT
    # thread's single counterpart; ROOM/BROADCAST/SUPPORT derive category
    # from the conversation's own kind instead (see CC-004's category
    # taxonomy — this is the "cheap fix, big UX unlock" the gap analysis
    # flagged: teacher_type/roles already existed, the serializer just
    # didn't expose a single category field the frontend could switch on).
    counterpart = None
    blocking = {"i_blocked": False, "blocked_me": False}
    can_block_flag = False
    category = "other"
    course_badge = None

    if conv.kind == Conversation.KIND_ROOM and conv.context_type == "course":
        category = "courses"
        course_badge = _course_badge(conv.context_id)
    elif conv.kind == Conversation.KIND_BROADCAST:
        category = "announcements"
        if conv.context_type == "course":
            course_badge = _course_badge(conv.context_id)
    elif conv.kind == Conversation.KIND_SUPPORT:
        category = "support"
    elif conv.kind == Conversation.KIND_DIRECT and others:
        cp = others[0]
        counterpart = _participant_dict(cp, include_presence=True, include_last_read=True)
        if me_participant:
            a_kind, a_id = me_participant.kind, _participant_obj_id(me_participant)
            b_kind, b_id = cp.kind, _participant_obj_id(cp)
            i_blocked, blocked_me = block_state(a_kind, a_id, b_kind, b_id)
            blocking = {"i_blocked": i_blocked, "blocked_me": blocked_me}
            can_block_flag = can_block(me_participant.kind, cp.kind)
        if cp.kind == Participant.KIND_TEACHER:
            roles = participant_roles(cp)
            category = "guest_experts" if ("guest" in roles and "faculty" not in roles) else "faculty"
        elif cp.kind == Participant.KIND_STAFF:
            category = "support"
        else:
            category = "students"

    is_course_room = conv.kind == Conversation.KIND_ROOM and conv.context_type == "course"
    track = course_room_track(conv.course_id) if is_course_room else None

    me_dict = None
    pinned = False
    archived = False
    muted_until_iso = None
    can_post_flag = False
    if me_participant:
        me_dict = {
            "id": str(me_participant.id),
            "identity": me_participant.identity_key(),
            "kind": me_participant.kind,
            "roles": participant_roles(me_participant),
        }
        pinned = bool(me_participant.pinned)
        archived = me_participant.archived_at is not None
        if me_participant.muted_until and me_participant.muted_until > timezone.now():
            muted_until_iso = me_participant.muted_until.isoformat()
        can_post_flag = policy.can_post(conv, me_participant)[0]

    return {
        "id": str(conv.id),
        "kind": conv.kind,
        "category": category,
        "track": track,
        "title": conv.title or (others[0].display_name() if others else ""),
        "course_id": str(conv.course_id) if conv.course_id else None,
        "course": course_badge,
        "participant_count": len(parts),
        "participants": [_participant_dict(p) for p in parts],
        "me": me_dict,
        "pinned": pinned,
        "archived": archived,
        "muted_until": muted_until_iso,
        "counterpart": counterpart,
        "blocking": blocking,
        "can_block": can_block_flag,
        "can_post": can_post_flag,
        "last_message": serialize_message(last) if last else None,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "unread": unread,
    }
