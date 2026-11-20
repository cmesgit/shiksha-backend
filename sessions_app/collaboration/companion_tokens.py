"""The companion's LiveKit identity and its screen-only token (RC-009a).

Spec: `companion-architecture.md`, `architecture-decisions.md` ADR-02 as
revised by `adr-02-revision-proposal.md` (**Option C, decided 11 November
2026**), and `api-contracts.md` §Participant and capture APIs.

🔴 **The companion's token grants `can_subscribe: true`, and that is a
decision, not an oversight.** ADR-02 originally specified
`can_publish_data: true` with `can_subscribe: false`, so the companion could
be driven but could never receive anyone's camera or microphone. RC-003
established that LiveKit has no such split — `canSubscribe` governs tracks
generally, there is no separate grant for receiving data, and upstream
treats data surviving a `canSubscribe` revocation as a bug to fix. A
companion with `can_subscribe: false` would be **deaf**: it would receive no
input at all.

Option C's reasoning, which is worth keeping next to the code: the threat
model already concedes that a compromised host OS or a maliciously modified
companion is outside the guarantee, so "our binary could subscribe" was
never what the token defended against. What it defends against is
*something that is not our program* holding the credential. So media
isolation becomes client-side (connect with auto-subscribe off, never
instantiate an audio sink), and authority for input moves into the signed
epoch and sequence floors the companion verifies cryptographically, rather
than into an SDK-reported sender name.

What the token still enforces server-side, and must keep enforcing:

* **publish screen only** — no camera, no microphone. Expressed through
  `can_publish_sources` where the installed SDK supports it.
* **one room** — the room this host binding belongs to, nothing else.
* **a short join TTL** — the token is for joining, not for standing
  authority.

🔴 **An `rcdev_` prefix proves nothing.** The prefix exists so human
presence accounting can *exclude* a companion cheaply and fail-safe. It must
never be the thing that makes a participant *trusted* as a companion — that
requires a live registered host binding whose device matches. RC-009's
acceptance criterion says so directly: "Prevent unknown prefixed identities
being treated as valid companion." The two directions are deliberately
asymmetric; see `looks_like_companion` and `registered_companion`.
"""
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

#: Reserved identity prefix. Human group-session identities are
#: ``{user.id}_{session.id}`` — an integer then an underscore — so this
#: prefix cannot collide with one.
COMPANION_PREFIX = "rcdev_"

#: `join_expires_in: 60` from the host-bindings share row.
JOIN_TOKEN_TTL_SECONDS = 60

#: Only this source. Screen share audio is deliberately excluded: the
#: feature captures a display, and a token that could publish system audio
#: would be a wider capability than anyone consented to.
PUBLISH_SOURCES = ("screen_share",)

_IDENTITY_RE = re.compile(r"^rcdev_[0-9a-f-]{36}$")


def companion_identity(host_binding_id):
    """The identity a companion joins with: ``rcdev_<host_binding_uuid>``.

    Binding the identity to the *host binding* rather than to the device or
    the user is what makes it single-purpose: it names one share of one
    display in one room, so it cannot be replayed into another room, and it
    becomes meaningless the moment that binding closes.
    """
    return f"{COMPANION_PREFIX}{host_binding_id}"


def looks_like_companion(identity):
    """Cheap, fail-safe test used only to EXCLUDE from human accounting.

    🔴 Deliberately permissive, and deliberately not a trust check. Anything
    carrying the reserved prefix is kept out of attendance, presence and
    capacity — including a forged one. That direction is safe: the worst a
    forgery achieves is excluding itself from a count it was never entitled
    to appear in.

    The opposite direction is never allowed to use this. See
    ``registered_companion``.
    """
    return bool(identity) and str(identity).startswith(COMPANION_PREFIX)


def well_formed_companion(identity):
    """Prefix *and* a syntactically valid binding id.

    Still not a trust check — it only rules out garbage before a store
    lookup, so a malformed identity cannot be used to probe the binding
    store.
    """
    return bool(identity) and bool(_IDENTITY_RE.match(str(identity)))


def host_binding_id_from_identity(identity):
    if not well_formed_companion(identity):
        return None
    return str(identity)[len(COMPANION_PREFIX):]


def registered_companion(identity, *, room_name=None):
    """The ONLY way an identity becomes a trusted companion.

    Returns the host-binding record, or ``None``. A caller that wants to
    grant a participant any companion privilege must go through here — the
    prefix alone, however well formed, confers nothing.

    ``room_name`` is checked when given, so a binding for one room cannot
    vouch for an identity seen in another.
    """
    from . import host_bindings

    binding_id = host_binding_id_from_identity(identity)
    if binding_id is None:
        return None
    try:
        record = host_bindings.get(binding_id)
    except host_bindings.StoreUnavailable:
        # Fail closed: "we cannot check" is not "yes".
        return None
    if record is None:
        return None
    # 🔴 A terminal record is kept briefly so a polling client learns *why*
    # its share ended rather than meeting a bare 404. That courtesy must not
    # leak into trust: a closed or failed share vouches for nothing. Without
    # this, a companion whose capture was stopped keeps its standing for the
    # 30 seconds the tombstone lives — which is precisely the "stale
    # credential" case RC-009's acceptance criteria name.
    if record.get("state") not in host_bindings.LIVE_STATES:
        return None
    if room_name is not None and record.get("room_name") != room_name:
        return None
    return record


class LiveKitUnconfigured(Exception):
    """No LiveKit credentials, so no token can be signed.

    🔴 Raised explicitly rather than letting the SDK's ``ValueError`` escape
    as a 500. A deployment missing credentials is a *dependency* problem —
    the companion should back off and retry, which a 503 tells it and an
    opaque server error does not. It also keeps the failure legible in logs
    instead of looking like a crash in the share path.
    """


def mint_join_token(record, *, display_name=None):
    """A 60-second, screen-only, single-room LiveKit token.

    Mirrors ``services/group_session_token.generate_group_session_token``'s
    construction so both tokens are built the same way against the same SDK,
    and differs only where the contract requires it.
    """
    from livekit.api import AccessToken, VideoGrants

    room_name = record.get("room_name")
    if not room_name:
        raise ValueError("host binding has no room_name")
    if not getattr(settings, "LIVEKIT_API_KEY", "") or \
            not getattr(settings, "LIVEKIT_API_SECRET", ""):
        raise LiveKitUnconfigured()

    identity = companion_identity(record["host_binding_id"])
    owner_name = record.get("owner_display_name") or "Desktop"

    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )
    token.with_identity(identity)
    # 🔴 Visible, and named so a human can recognise it. Option C's residual
    # risk — a stolen credential used by an attacker's client — is bounded
    # partly by the companion being an observable participant rather than a
    # silent one. A second, unexpected "… — Remote Companion" in the roster
    # is the signal that makes that risk detectable.
    token.with_name(display_name or f"{owner_name} — Remote Companion")
    token.with_ttl(_ttl())

    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        # See the module docstring: not a choice, a constraint of the SDK.
        # Media isolation is client-side under ADR-02 Option C.
        can_subscribe=True,
        can_publish_data=True,
    )
    _restrict_publish_sources(grants)
    token.with_grants(grants)
    return token.to_jwt(), identity


def _ttl():
    from datetime import timedelta
    return timedelta(seconds=JOIN_TOKEN_TTL_SECONDS)


def _restrict_publish_sources(grants):
    """Pin publishing to the screen, where the SDK can express it.

    🔴 Set defensively rather than passed to the constructor. The installed
    ``livekit-api`` may predate ``can_publish_sources``; passing it as a
    kwarg would raise at construction and take down the share route
    entirely, which is a worse failure than a token that is merely broader
    than intended. When it cannot be applied the fact is logged loudly
    rather than passing silently, because the difference matters: without
    it, nothing but our own client behaviour stops the companion publishing
    a camera.
    """
    if hasattr(grants, "can_publish_sources"):
        try:
            grants.can_publish_sources = list(PUBLISH_SOURCES)
            return True
        except Exception:  # noqa: BLE001
            pass
    logger.warning(
        "livekit VideoGrants has no usable can_publish_sources; the companion "
        "token cannot be restricted to screen_share server-side. Publishing "
        "restraint is client-side only until the SDK is upgraded."
    )
    return False
