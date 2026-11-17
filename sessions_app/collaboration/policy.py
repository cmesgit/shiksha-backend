"""Who may hold a collaboration binding, and in which rooms (RC-006).

This module answers exactly one question — *may this browser principal
register as an interactive participant of this session?* — and deliberately
answers nothing about control grants. Eligibility to **bind** is symmetric
(student↔student and teacher↔teacher both bind the same way); eligibility to
**request control** is the per-context rule from OD-01 and belongs to RC-008.

It reuses the existing room-membership rules rather than restating them:

* ``group_session_policy.policy_for(session)`` — the instant-vs-scheduled
  dispatcher, which is also where "instant has no course gate" already lives.
* ``group_session_views._chat_participant_check`` — the existing "may this
  user be in this room" predicate, shared with the chat consumer.

Restating either here would let the collaboration plane drift away from the
room it is supposed to describe.
"""
import uuid

from django.conf import settings

from global_settings.models import GlobalSettings


# ── Session adapters ──────────────────────────────────────────────────────
# `SessionRef = {kind, id}` with kind in group|private|live|skill. The
# contract says "Only group enabled initially", and the task's acceptance
# requires unknown adapters be DISABLED rather than quietly treated as group.
SESSION_KIND_GROUP = "group"
KNOWN_SESSION_KINDS = frozenset({"group", "private", "live", "skill"})
ENABLED_SESSION_KINDS = frozenset({SESSION_KIND_GROUP})

# The five-name allowlist from realtime-event-contracts.md §Capabilities.
# A binding advertises what the *browser* could ask for; the approved subset
# on a grant is RC-008's business.
ALL_CAPABILITIES = (
    "pointer.move",
    "pointer.button",
    "pointer.scroll",
    "keyboard.physical",
    "keyboard.text",
)


class PolicyError(Exception):
    """A refusal with the contract's error code and HTTP status already
    decided, so views never invent either."""

    def __init__(self, code, detail, status=403):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def feature_enabled():
    return bool(GlobalSettings.load().collaboration_bindings_enabled)


class Principal:
    """The browser actor, derived entirely from the auth token — never from
    the request body.

    ``auth_sid`` is the tracked ``UserSession`` id carried as the JWT ``sid``
    claim. The contract is explicit that it is mandatory here: *"Missing
    legacy auth sid means collaboration requires a normal login
    refresh/re-auth that supplies a tracked session; it never degrades to
    account-only authority."* A token minted before sid tracking existed must
    therefore be refused, not silently accepted with reduced binding.
    """

    __slots__ = ("user", "auth_sid", "context", "profile_id")

    def __init__(self, user, auth_sid, context, profile_id):
        self.user = user
        self.auth_sid = auth_sid
        self.context = context
        self.profile_id = profile_id

    @property
    def identity_tuple(self):
        """What a heartbeat must still match. A binding is pinned to the auth
        session and context it was created under, so switching profile or
        re-logging in invalidates it rather than inheriting its standing."""
        return (str(self.user.id), self.auth_sid, self.context, self.profile_id)


def principal_from_request(request):
    """Resolve the acting principal, or raise PolicyError.

    Reads the simplejwt token object on ``request.auth`` — the same place
    ``accounts.auth_flow.session_id_for`` and ``chat.services
    .active_identity_from_request`` read it, so there is no third resolver.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise PolicyError("unauthenticated", "Sign in to continue.", status=401)

    token = getattr(request, "auth", None)
    if token is None:
        raise PolicyError(
            "auth_session_required",
            "Collaboration needs a tracked sign-in. Sign in again to continue.",
            status=403,
        )

    auth_sid = token.get("sid") if hasattr(token, "get") else None
    if not auth_sid:
        # Deliberately NOT falling back to the account. See Principal's
        # docstring — degrading to account-only authority is the exact
        # failure the contract forbids.
        raise PolicyError(
            "auth_session_required",
            "Collaboration needs a tracked sign-in. Sign in again to continue.",
            status=403,
        )

    return Principal(
        user=user,
        auth_sid=str(auth_sid),
        context=token.get("context") or "",
        profile_id=str(token.get("active_profile") or "") or None,
    )


def resolve_session(kind, session_id):
    """Load the session a SessionRef points at, or raise PolicyError.

    An unknown-but-named kind and an outright invalid kind are different
    refusals: the first is a capability we have not shipped (422), the second
    is a malformed body (400).
    """
    if not kind or kind not in KNOWN_SESSION_KINDS:
        raise PolicyError("invalid_session_kind", "Unknown session kind.", status=400)
    if kind not in ENABLED_SESSION_KINDS:
        raise PolicyError(
            "unsupported_session_kind",
            f"Collaboration is not available for {kind} sessions yet.",
            status=422,
        )

    from sessions_app.models import GroupSession

    try:
        uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        raise PolicyError("invalid_session_id", "Malformed session id.", status=400)

    session = GroupSession.objects.filter(id=session_id).first()
    if session is None:
        # 404 covers "nonexistent OR inaccessible" per the contract, so a
        # stranger cannot distinguish a real room from a fake one.
        raise PolicyError("not_found", "No such session.", status=404)
    return session


def assert_may_bind(principal, session):
    """Raise PolicyError unless this principal may bind to this room.

    Three independent gates, in increasing cost order:

    1. The room must still be joinable at all (it must have a LiveKit room).
    2. The existing chat-participant rule must pass — host, or instant (no
       course gate), or an accepted invite.
    3. Presence must not be stale: a ``GroupSessionParticipant`` row whose
       ``left_at`` is set means this user left, and a left participant has no
       standing even though their account is still a member of the room.

    Live presence itself is proven separately, by the LiveKit SID check in
    ``bindings.verify_participant_sid`` — membership says *allowed to be
    here*, the SID says *actually here now*.
    """
    from sessions_app.group_session_views import _chat_participant_check
    from sessions_app.models import GroupSessionParticipant

    if not getattr(session, "room_name", None):
        raise PolicyError(
            "room_not_open", "This room has not been opened yet.", status=409
        )

    allowed, _ = _chat_participant_check(session, principal.user)
    if not allowed:
        raise PolicyError("not_a_participant", "You are not in this room.", status=404)

    # Stale presence. The host is exempt: _end_group_session_internal closes
    # every participant row including theirs, and a host re-opening their own
    # room must not be locked out by their own previous exit.
    if principal.user.id != session.host_id:
        row = (
            GroupSessionParticipant.objects
            .filter(session=session, user=principal.user)
            .order_by("-joined_at")
            .first()
        )
        if row is not None and row.left_at is not None:
            raise PolicyError(
                "presence_stale",
                "You have left this room. Rejoin to continue.",
                status=409,
            )


def livekit_identity(user, session):
    """The identity the existing join flow already minted for this user.

    Must stay byte-identical to
    ``services/group_session_token.generate_group_session_token``'s
    ``with_identity`` — it is what LiveKit will report, and the SID check
    matches on it.
    """
    return f"{user.id}_{session.id}"


def capabilities_for(principal, session):
    """What this binding may later *ask* for. Not an approval."""
    return list(ALL_CAPABILITIES)


def allowed_origins():
    """Exact origins a collaboration browser request may come from.

    Defaults to the per-environment ``CORS_ALLOWED_ORIGINS`` — the list of
    real front-end apps — rather than ``CSRF_TRUSTED_ORIGINS``, which also
    contains the API's own host and is a looser set. Overridable so a future
    rollout can narrow it further without touching CORS.

    Matching is exact string equality against the full origin. Never a suffix
    test: ``evil-shikshacom.com`` and ``shikshacom.com.attacker.net`` both
    pass a naive ``endswith``.
    """
    explicit = getattr(settings, "COLLABORATION_ALLOWED_ORIGINS", None)
    if explicit is not None:
        return frozenset(explicit)
    return frozenset(getattr(settings, "CORS_ALLOWED_ORIGINS", ()) or ())
