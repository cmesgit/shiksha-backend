# PLACEMENT: backend/backend/chat/policy.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/policy.py
"""
chat/policy.py — the one authorization gate for chat (Phase 3 §10).

Three related, independent decisions live here:

  1. ROOM MEMBERSHIP — can_join_room(context_type, context_id, kind, obj).
     Backs the generalized services.ensure_room()/ensure_course_room(). A
     capability-provider registry (register_provider/get_provider) means
     chat never imports a vertical's models to decide who may join a room
     of that vertical's context_type — only the vertical registers a
     membership function. chat/apps.py's ready() registers "course" by
     delegating straight to the ALREADY-EXISTING
     chat.services.can_join_course_room (not rewritten).
     FAIL-CLOSED: an unregistered or unknown context_type is denied — e.g.
     "counseling_case" has no provider registered until M4, so nothing can
     join a counseling room yet, even though the context_type is now a
     valid value a Conversation can carry. This is the opposite of M0's
     Redis helpers, which fail OPEN, deliberately: a stale unread badge on
     a Redis outage is cosmetic, but letting someone into a room because
     nobody had registered a checker yet is a real access-control hole.

  2. STARTING A DIRECT MESSAGE — can_start_dm(a_kind, a_obj, b_kind, b_obj).
     A static matrix keyed on the single-letter accounts.Identity kinds
     (L/T/C/R/S) — NOT chat.Participant's own LEARNER/TEACHER strings —
     so the matrix already makes sense for identity kinds chat.Participant
     doesn't support as a participant yet (Counsellor, Recruiter), the same
     future-proofing accounts.Identity itself was built with. Wired into
     chat/views.py's StartDirectView as a check-then-create, the same shape
     CourseRoomView already uses with can_join_course_room.

  3. POSTING INTO AN EXISTING CONVERSATION — can_post(conversation, kind,
     obj). A structural gate — frozen conversation, read-only broadcast —
     wired into services.post_message_checked() BEFORE moderation and
     blocking. This is NOT a membership check (the Participant row already
     exists by the time a post is attempted); it's "is this conversation,
     as a whole, postable into right now," independent of who's asking.

IMPORT DIRECTION: this module calls into chat/services.py for the data
lookups a couple of the DM rules need (public-faculty / shared-course /
shared-room), but does so with a LOCAL import inside each function rather
than a top-level one — services.py imports THIS module at its top (to call
can_post() from post_message_checked()), so a top-level import here would
be circular.
Same lazy-import discipline chat/services.py already uses for its own
cross-app lookups (learner_in_course(), etc.), applied to a cross-module
cycle instead of a cross-app one.
"""
import logging

from accounts.models import Identity
from .models import Conversation, Participant

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1) Capability-provider registry + room membership
# ---------------------------------------------------------------------------

_PROVIDERS = {}


def register_provider(context_type, membership_fn):
    """membership_fn(kind, obj, context_id) -> bool. Called once per
    context_type, typically from an AppConfig.ready() (see chat/apps.py)."""
    if context_type in _PROVIDERS:
        logger.warning(
            "chat.policy: provider for context_type=%r registered more than "
            "once — overwriting", context_type,
        )
    _PROVIDERS[context_type] = membership_fn


def get_provider(context_type):
    return _PROVIDERS.get(context_type)


def can_join_room(context_type, context_id, kind, obj):
    """True iff `obj` (a LearnerProfile/TeacherProfile, per `kind`) may be
    attached as a participant of the ROOM identified by
    (context_type, context_id). Fail-closed: see the module docstring."""
    provider = get_provider(context_type)
    if provider is None:
        logger.info(
            "chat.policy: can_join_room denied — no provider registered for "
            "context_type=%r (fail-closed)", context_type,
        )
        return False
    try:
        return bool(provider(kind, obj, context_id))
    except Exception:
        logger.exception(
            "chat.policy: provider for context_type=%r raised — failing "
            "closed", context_type,
        )
        return False


# ---------------------------------------------------------------------------
# 2) Starting a direct message — the DM matrix
# ---------------------------------------------------------------------------

DM_ALWAYS = "ALWAYS"                  # may always DM each other
DM_IF_RELATIONSHIP = "IF_RELATIONSHIP"  # only if some existing relationship holds
DM_SAME_ROOM_ONLY = "SAME_ROOM_ONLY"    # only if already co-members of a ROOM
DM_NEVER = "NEVER"                      # not available yet / not by design

# Keyed by an UNORDERED pair of accounts.Identity kind letters — a
# frozenset, so {L, T} and {T, L} are the same key, and a same-kind pair
# like T<->T collapses to the single-element frozenset({T}).
#
# Only L<->T, L<->L, and T<->T are spelled out here (see the message this
# stage started from); everything touching C (Counsellor) or R (Recruiter)
# defaults to NEVER below — the Counselling relationship isn't defined
# until M4's booking flow, and the Placement vertical (R) isn't live at
# all yet (Phase 3 §22, reserved). S (system/announcement identities)
# defaults to NEVER too: a system sender uses a SUPPORT/BROADCAST room,
# never a DIRECT thread. None of this is stated as a hard requirement
# anywhere upstream of this stage — it's the safe, fail-closed default for
# every pair that wasn't specified, matching this module's overall
# fail-closed posture, and is easy to widen with one line per pair once a
# vertical actually defines its own DM rule.
DM_MATRIX = {
    frozenset({Identity.KIND_LEARNER, Identity.KIND_TEACHER}): DM_IF_RELATIONSHIP,
    frozenset({Identity.KIND_LEARNER}): DM_SAME_ROOM_ONLY,   # learner <-> learner
    frozenset({Identity.KIND_TEACHER}): DM_ALWAYS,           # teacher <-> teacher
}
_DEFAULT_DM_RULE = DM_NEVER

_NO_RELATIONSHIP_REASON = "This teacher isn't available to message yet."
_NOT_SAME_ROOM_REASON = "You can message another student once you're both in the same class chat."
_NOT_AVAILABLE_REASON = "Starting a chat with this account isn't available yet."


def _dm_rule(letter_a, letter_b):
    return DM_MATRIX.get(frozenset({letter_a, letter_b}), _DEFAULT_DM_RULE)


def can_start_dm(a_kind, a_obj, b_kind, b_obj):
    """a_kind/b_kind are chat.Participant kind strings ("LEARNER"/
    "TEACHER") — converted to accounts.Identity's single-letter kinds for
    the matrix lookup, same conversion Identity.kind_for_participant_kind()
    already provides for exactly this purpose.

    Returns (allowed: bool, reason: str). `reason` is user-facing and only
    meaningful when allowed=False.
    """
    letter_a = Identity.kind_for_participant_kind(a_kind)
    letter_b = Identity.kind_for_participant_kind(b_kind)
    rule = _dm_rule(letter_a, letter_b)

    if rule == DM_ALWAYS:
        return True, ""

    if rule == DM_IF_RELATIONSHIP:
        from . import services  # local: see module docstring
        learner_obj = a_obj if a_kind == "LEARNER" else b_obj
        teacher_obj = a_obj if a_kind == "TEACHER" else b_obj
        if services.teacher_is_public_faculty(teacher_obj):
            return True, ""
        if services.learner_teacher_share_active_course(learner_obj, teacher_obj):
            return True, ""
        return False, _NO_RELATIONSHIP_REASON

    if rule == DM_SAME_ROOM_ONLY:
        from . import services  # local: see module docstring
        if services.learners_share_room(a_obj, b_obj):
            return True, ""
        return False, _NOT_SAME_ROOM_REASON

    return False, _NOT_AVAILABLE_REASON


# ---------------------------------------------------------------------------
# 3) Posting into an existing conversation — the structural gate
# ---------------------------------------------------------------------------

_FROZEN_REASON = "This conversation is closed and no longer accepts new messages."
_BROADCAST_REASON = "This is a broadcast channel — only the course's teachers can post here."
_SUSPENDED_REASON = (
    "Your ability to send messages has been temporarily restricted by a "
    "platform moderator."
)
_MEMBERSHIP_LAPSED_REASON = (
    "You no longer have access to this conversation."
)


def can_post(conversation, participant):
    """Structural gate wired into services.post_message_checked() BEFORE
    moderation/blocking.

    Rules:
      1. is_frozen        → nobody may post (unchanged from M3).
      2. kind == BROADCAST → read-only for everyone EXCEPT a TEACHER
         participant (Stage D / CC-015: Announcements). M3 shipped this as
         unconditionally read-only with a note that a future rule would need
         to know who's asking — this is that rule. A LEARNER or STAFF
         participant of a BROADCAST room (a course's enrolled students; a
         support agent has no reason to be in one) still cannot post.
      3. Course membership re-check — a Participant row proves someone once
         joined a course room, not that their Enrollment/TeachingAssignment
         is still active (that's checked only at join time otherwise, so
         access would otherwise survive indefinitely past e.g. a refund or
         a teacher reassignment). Local import: see module docstring.

    Returns (allowed: bool, reason: str).
    """
    if conversation.is_frozen:
        return False, _FROZEN_REASON

    if conversation.kind == Conversation.KIND_BROADCAST:
        if participant is not None and participant.kind == Participant.KIND_TEACHER:
            pass
        else:
            return False, _BROADCAST_REASON

    from . import services
    if participant is not None and not services.is_course_membership_still_valid(
        conversation, participant
    ):
        return False, _MEMBERSHIP_LAPSED_REASON

    return True, ""
