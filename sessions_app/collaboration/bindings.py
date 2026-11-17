"""Expiring participant bindings, and the LiveKit SID check behind them.

A *binding* is the server's record that a specific browser tab, under a
specific tracked auth session, is an interactive participant of a specific
room right now. It is the thing every later collaboration call is scoped to.

Two properties make it safe, and both are load-bearing:

**It expires on its own.** TTL 6s, renewed by a 2s heartbeat. Nothing has to
notice a closed tab, a killed browser or a dropped network — authority simply
stops. That is why the store is Redis and not a table.

🔴 **It never writes to Postgres.** The box's capacity wall is
``max_connections`` (~75) and ASGI leaks roughly one connection per request;
a 2s-per-participant heartbeat that did a row write would exhaust it at
around 20 concurrent sessions. Heartbeats here are a DB **read** (membership)
plus Redis. If you ever find yourself adding an ``UPDATE`` to this module,
that is the bug.

🔴 **This module FAILS CLOSED, inverting the house style.** Every other cache
helper in this repo — ``accounts/revocation.py`` and ``chat/redis_utils.py``
— documents itself as failing *open* so an outage degrades availability
rather than access. Here an outage must remove authority, not grant it: a
binding that cannot be read is a binding that cannot be trusted, and the
caller gets 503. A reviewer who "fixes" this to match the other modules
turns a Redis outage into unauthenticated remote control.
"""
import json
import logging
import secrets
import time
import uuid

from django.core.cache import cache

logger = logging.getLogger(__name__)

# api-contracts.md: `expires_in: 6`, heartbeat "every 2s".
BINDING_TTL_SECONDS = 6
BINDING_HEARTBEAT_SECONDS = 2

# ws-ticket: "one-use", `expires_in: 30`.
TICKET_TTL_SECONDS = 30

# 🔴 Trap: coalesce LiveKit room queries to at most one per ROOM per 2s,
# never one per event. With a 2s heartbeat per participant, a per-request
# lookup would mean N LiveKit round trips every 2s for an N-person room.
ROOM_ROSTER_TTL_SECONDS = 2

_BINDING_PREFIX = "collab:binding:"
_ROSTER_PREFIX = "collab:lkroster:"
_TICKET_PREFIX = "collab:ticket:"


class StoreUnavailable(Exception):
    """The binding store could not be reached. Always a 503 — never silently
    treated as 'no such binding', which would read as a clean 404 and hide an
    outage behind a plausible answer."""


class RosterUnavailable(Exception):
    """LiveKit could not be asked who is in the room. 503, and specifically
    NOT 403: 'we cannot check your SID' and 'your SID is wrong' are different
    answers and the client retries only one of them."""


def _key(binding_id):
    return f"{_BINDING_PREFIX}{binding_id}"


# ── Binding lifecycle ─────────────────────────────────────────────────────


def create(principal, session, participant_sid, identity, capabilities):
    """Store a new binding and return (binding_id, record)."""
    binding_id = str(uuid.uuid4())
    record = {
        "binding_id": binding_id,
        "user_id": str(principal.user.id),
        "auth_sid": principal.auth_sid,
        "context": principal.context,
        "profile_id": principal.profile_id,
        "session_kind": "group",
        "session_id": str(session.id),
        "room_name": session.room_name,
        "identity": identity,
        "participant_sid": participant_sid,
        "capabilities": list(capabilities),
        "created_at": time.time(),
    }
    _write(binding_id, record)
    return binding_id, record


def _write(binding_id, record):
    try:
        cache.set(_key(binding_id), json.dumps(record), timeout=BINDING_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — any backend error is fail-closed
        logger.warning("collaboration binding store unavailable on write: %s", exc)
        raise StoreUnavailable() from exc


def get(binding_id):
    """The live record, or None if it expired or never existed.

    Raises StoreUnavailable on a backend error. The distinction matters: a
    missing key means the binding lapsed (the client should re-bind), an
    unreachable store means we do not know (the client should retry).
    """
    try:
        raw = cache.get(_key(binding_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("collaboration binding store unavailable on read: %s", exc)
        raise StoreUnavailable() from exc
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # A corrupt value is not a usable authorization. Drop it rather than
        # guess at its shape.
        logger.warning("discarding malformed collaboration binding %s", binding_id)
        delete(binding_id)
        return None


def renew(binding_id, record):
    """Re-set the same record at a full TTL. A plain SET, not an EXPIRE, so
    the renewed value always reflects the membership we just re-verified."""
    _write(binding_id, record)


def delete(binding_id):
    try:
        cache.delete(_key(binding_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("collaboration binding store unavailable on delete: %s", exc)
        raise StoreUnavailable() from exc


def owned_by(record, principal):
    """True iff this principal is still the exact actor the binding was
    issued to — same user, same tracked auth session, same context and
    profile. Re-logging in or switching profile does not inherit a binding.
    """
    if record is None:
        return False
    return (
        record.get("user_id") == str(principal.user.id)
        and record.get("auth_sid") == principal.auth_sid
        and record.get("context") == principal.context
        and record.get("profile_id") == principal.profile_id
    )


# ── LiveKit SID verification ──────────────────────────────────────────────


def _fetch_room_roster(room_name):
    """{identity: sid} for everyone LiveKit currently reports in the room.

    Isolated so tests can patch exactly this function: there are no LiveKit
    credentials in local or CI environments, so the network call itself is
    never exercised here. ``livestream/tasks._livekit_room_identities`` does
    the same listing but discards ``p.sid``, which is the one field this
    check exists to read — hence a separate helper rather than reuse.
    """
    from asgiref.sync import async_to_sync
    from django.conf import settings
    from livekit import api as lk_api
    from livekit.protocol.room import ListParticipantsRequest

    if not (settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET):
        raise RosterUnavailable("LiveKit is not configured")

    async def _run():
        client = lk_api.LiveKitAPI(
            settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET
        )
        try:
            resp = await client.room.list_participants(
                ListParticipantsRequest(room=room_name)
            )
            return {p.identity: p.sid for p in resp.participants}
        finally:
            await client.aclose()

    try:
        return async_to_sync(_run)()
    except Exception as exc:  # noqa: BLE001
        raise RosterUnavailable(str(exc)) from exc


def room_roster(room_name, force=False):
    """Cached {identity: sid}, at most one LiveKit call per room per 2s."""
    cache_key = f"{_ROSTER_PREFIX}{room_name}"
    if not force:
        try:
            cached = cache.get(cache_key)
        except Exception:  # noqa: BLE001
            cached = None  # a cache miss here is a performance problem, not
                           # a security one — fall through to the live call
        if cached is not None:
            return json.loads(cached)

    roster = _fetch_room_roster(room_name)
    try:
        cache.set(cache_key, json.dumps(roster), timeout=ROOM_ROSTER_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return roster


def verify_participant_sid(room_name, identity, claimed_sid, allow_refresh=False):
    """True iff LiveKit says `identity` is in `room_name` holding `claimed_sid`.

    Raises RosterUnavailable if LiveKit cannot be asked. Callers must map
    that to 503 and the False return to 403 — conflating them tells a user
    their session is invalid when the truth is that a dependency is down.

    ``allow_refresh`` re-asks LiveKit once when the identity is **absent**
    from a cached roster. Without it, anyone who joins the room during a live
    2s coalescing window is refused on their first bind, and "just retry" is
    not part of this endpoint's contract. The refresh stays bounded because
    it only fires on an absent identity (not a merely mismatched SID), only
    on binding creation — which is once per tab, and separately rate-limited
    — and never on the 2s heartbeat path, which does not call this at all.
    """
    roster = room_roster(room_name)
    actual = roster.get(identity)
    if actual is None and allow_refresh:
        actual = room_roster(room_name, force=True).get(identity)
    if actual is None:
        return False
    return secrets.compare_digest(str(actual), str(claimed_sid))


# ── Single-use WebSocket tickets ──────────────────────────────────────────
#
# Minted here so RC-006b's consumer has nothing to invent; the consumer that
# consumes them lands with the WS half.


def mint_ticket(binding_id, record):
    ticket = secrets.token_urlsafe(32)
    payload = {
        "binding_id": binding_id,
        "user_id": record["user_id"],
        "auth_sid": record["auth_sid"],
        "session_id": record["session_id"],
    }
    try:
        cache.set(
            f"{_TICKET_PREFIX}{ticket}", json.dumps(payload), timeout=TICKET_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("collaboration ticket store unavailable: %s", exc)
        raise StoreUnavailable() from exc
    return ticket


def consume_ticket(ticket):
    """Return the payload and atomically invalidate the ticket, or None.

    ``cache.delete`` returning falsey means somebody else consumed it first,
    so the loser of that race gets None — one use, exactly.
    """
    key = f"{_TICKET_PREFIX}{ticket}"
    try:
        raw = cache.get(key)
        if raw is None:
            return None
        cache.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("collaboration ticket store unavailable: %s", exc)
        raise StoreUnavailable() from exc
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
