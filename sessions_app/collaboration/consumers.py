"""The collaboration notification socket (RC-006b).

``/ws/collaboration/v1/`` — a **ticket-authenticated, receive-mostly** socket
whose entire job is to tell a bound tab that something it already has access
to has changed. REST stays authoritative; this is an invalidation bus.

What it is allowed to carry, from `realtime-event-contracts.md`:

    "Pings/invalidation only; REST remains authoritative."
    "New WS cannot send arbitrary input or consent actions."

So the inbound vocabulary is exactly two frames — ``authenticate`` once, then
``ping`` — and *everything* else closes the socket. Pointer and keyboard
input never touch this path; it rides the LiveKit data channel under ADR-02
Option C. If a future change adds an inbound verb here, that is a new
authorization surface and needs the contract owner, not a patch.

Five things are easy to get wrong and are therefore spelled out:

🔴 **This consumer does its own exact-Origin check, and must.** ``config/
asgi.py`` wraps the websocket stack in ``JWTAuthMiddleware`` and **no**
``OriginValidator`` — combined with cookies that are ``SameSite=None;
Domain=.shikshacom.com``, any origin on the internet can already open an
*authenticated* socket to the existing chat consumers. That is pre-existing
and out of scope to change here (wrapping the shared middleware would alter
behaviour for every existing socket in the repo, silently). This route
refuses to inherit the hole.

🔴 **A missing Origin is not a free pass.** The contract allows the *native*
route to omit Origin, because a device is identified by its device ticket.
Today no device ticket can exist — RC-007 owns device pairing — so every
ticket is a browser ticket and a browser ticket demands an allowlisted
Origin. The check is deferred to authenticate time rather than the handshake
precisely so RC-007 can land the device case without reopening this: the
ticket says which rule applies.

🔴 **It never touches attendance.** ``GroupSessionChatConsumer`` increments
``active_connections`` (the room-capacity gate) and guards that with a
``self.counted`` flag. This socket is registration *after* the join, not a
second presence — if it counted, opening the control UI would consume a seat
and push real invitees into ``409 room_full``.

🔴 **The ticket is never logged.** Not on success, not on failure, not in a
rejection reason. "Never put it in URL/subprotocol logs" extends to ours.

🔴 **A ping is a cache read, never a DB write.** The binding TTL is 6s and a
ticket lives 30s, so a socket can outlive the binding that justified it. Each
ping re-reads the binding from Redis and drops the socket if it has lapsed —
which also means the socket cannot outlive its binding by more than one ping
interval. Consequence for RC-008: **authorization must be re-checked when an
event is published**, never inferred from group membership.
"""
import asyncio
import json
import logging
import time
from collections import deque

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from . import bindings as binding_store
from . import events
from .policy import allowed_origins, feature_enabled

logger = logging.getLogger(__name__)

# "Ticket is passed in the first WS frame … within 5s".
AUTH_DEADLINE_SECONDS = 5

# "Apply max frame 8KiB/rate 20 per second".
MAX_FRAME_BYTES = 8 * 1024
MAX_FRAMES_PER_SECOND = 20

# Application close codes (4000–4999 is the private range). They mirror the
# REST plane's statuses so a client can share one error vocabulary.
CLOSE_PROTOCOL_ERROR = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_FEATURE_OFF = 4404
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_FRAME_TOO_LARGE = 4413
CLOSE_RATE_LIMITED = 4429
CLOSE_DEPENDENCY_UNAVAILABLE = 4503


class CollaborationEventConsumer(AsyncWebsocketConsumer):
    """Receives nothing but ``authenticate`` and ``ping``; sends invalidations."""

    async def connect(self):
        self.authenticated = False
        self.binding_id = None
        self.joined_groups = []
        self._auth_timer = None
        self._frame_times = deque()

        # Mirrors the REST plane: while the flag is off the route behaves as
        # though it does not exist, so the surface is not probeable.
        if not await database_sync_to_async(feature_enabled)():
            await self.close(code=CLOSE_FEATURE_OFF)
            return

        origin = self._origin()
        self.origin_present = origin is not None
        if self.origin_present and origin not in allowed_origins():
            # Exact string equality only. `evilapp.test.shikshacom.com` ends
            # with an allowed host and defeats any `endswith` implementation.
            await self.close(code=CLOSE_FORBIDDEN)
            return

        await self.accept()
        self._auth_timer = asyncio.ensure_future(self._close_if_unauthenticated())

    def _origin(self):
        for name, value in self.scope.get("headers", []):
            if name == b"origin":
                return value.decode("utf-8", errors="ignore")
        return None

    async def _close_if_unauthenticated(self):
        """The 5s deadline. An unauthenticated socket is not a free resource:
        it holds a worker slot and a channel-layer registration."""
        try:
            await asyncio.sleep(AUTH_DEADLINE_SECONDS)
        except asyncio.CancelledError:
            return
        if not self.authenticated:
            await self.close(code=CLOSE_AUTH_TIMEOUT)

    async def disconnect(self, close_code):
        if self._auth_timer is not None:
            self._auth_timer.cancel()
        for group in self.joined_groups:
            await self.channel_layer.group_discard(group, self.channel_name)
        # 🔴 Deliberately nothing else. No attendance counter, no participant
        # row, no auto-expire timer — see this module's docstring.

    # ── inbound ───────────────────────────────────────────────────────────

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None or text_data is None:
            # The contract is a JSON text protocol; a binary frame here is
            # either a confused client or someone probing for an input path.
            await self.close(code=CLOSE_PROTOCOL_ERROR)
            return

        if len(text_data.encode("utf-8")) > MAX_FRAME_BYTES:
            # Measured in BYTES, not characters: a multi-byte payload passes a
            # naive len() ceiling at roughly a quarter of the real size.
            await self.close(code=CLOSE_FRAME_TOO_LARGE)
            return

        if not self._allow_frame():
            await self.close(code=CLOSE_RATE_LIMITED)
            return

        try:
            payload = json.loads(text_data)
        except ValueError:
            await self.close(code=CLOSE_PROTOCOL_ERROR)
            return

        if not isinstance(payload, dict) or payload.get("v") != events.ENVELOPE_VERSION:
            await self.close(code=CLOSE_PROTOCOL_ERROR)
            return

        frame_type = payload.get("type")

        if not self.authenticated:
            if frame_type != "authenticate":
                # Nothing is readable before the ticket is presented.
                await self.close(code=CLOSE_UNAUTHENTICATED)
                return
            await self._authenticate(payload)
            return

        if frame_type == "ping":
            await self._ping()
            return

        # 🔴 The refusal that matters. There is no inbound verb for input,
        # consent, grant mutation or subscription: those are REST, with their
        # own authorization. A client that tries one loses the socket.
        await self.close(code=CLOSE_PROTOCOL_ERROR)

    def _allow_frame(self):
        """20 frames per rolling second."""
        now = time.monotonic()
        while self._frame_times and now - self._frame_times[0] >= 1.0:
            self._frame_times.popleft()
        if len(self._frame_times) >= MAX_FRAMES_PER_SECOND:
            return False
        self._frame_times.append(now)
        return True

    # ── authentication ────────────────────────────────────────────────────

    async def _authenticate(self, payload):
        if set(payload) - {"v", "type", "ticket"}:
            await self.close(code=CLOSE_PROTOCOL_ERROR)
            return
        ticket = payload.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            await self.close(code=CLOSE_PROTOCOL_ERROR)
            return

        try:
            claims = await sync_to_async(binding_store.consume_ticket)(ticket)
        except binding_store.StoreUnavailable:
            await self.close(code=CLOSE_DEPENDENCY_UNAVAILABLE)
            return
        if claims is None:
            # Unknown, already used, or expired — one answer for all three, so
            # a ticket cannot be probed for liveness.
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        binding_id = claims.get("binding_id")
        if not binding_id:
            # A device ticket (``host_binding_id``). RC-007 mints those and
            # RC-009 gives them a room; until then a ticket without a browser
            # binding cannot be honoured. Failing closed here is what keeps
            # the Origin exemption below from being reachable early.
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        # 🔴 Browser ticket ⇒ Origin was mandatory. Checked here rather than at
        # the handshake so the device case stays implementable later.
        if not self.origin_present:
            await self.close(code=CLOSE_FORBIDDEN)
            return

        try:
            record = await sync_to_async(binding_store.get)(binding_id)
        except binding_store.StoreUnavailable:
            await self.close(code=CLOSE_DEPENDENCY_UNAVAILABLE)
            return
        if record is None:
            # The binding lapsed between minting (30s ticket) and use (6s
            # binding). Authority follows the binding, not the ticket.
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        # The ticket must still describe the binding it was minted for. A
        # binding id is a UUID and tickets are single-use, so this is belt and
        # braces — but it is the check that makes "re-login invalidates the
        # socket" true rather than merely likely.
        if (record.get("user_id") != claims.get("user_id")
                or record.get("auth_sid") != claims.get("auth_sid")):
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        # Defence in depth, not the primary gate. The ticket IS the
        # authentication per the contract (a device has no cookie at all), but
        # when the handshake did carry a session cookie, a mismatch means a
        # ticket is being replayed from somebody else's browser.
        scope_user = self.scope.get("user")
        if scope_user is not None and getattr(scope_user, "is_authenticated", False):
            if str(getattr(scope_user, "id", "")) != str(record.get("user_id")):
                await self.close(code=CLOSE_FORBIDDEN)
                return

        self.authenticated = True
        self.binding_id = binding_id
        if self._auth_timer is not None:
            self._auth_timer.cancel()

        self.joined_groups = [
            events.binding_group(binding_id),
            events.principal_group(record.get("user_id"), record.get("context")),
        ]
        for group in self.joined_groups:
            await self.channel_layer.group_add(group, self.channel_name)

        # ⚠️ `authenticated` and `pong` are handshake acks, not events — the
        # contract's envelope describes server→client *notifications*, and it
        # defines no success reply. Without an ack a client cannot distinguish
        # "authenticated" from "silently ignored", so one is sent. Recorded as
        # a documented addition in evidence/collaboration-socket.md rather
        # than folded in as if the pack had specified it.
        await self.send(text_data=json.dumps({
            "v": events.ENVELOPE_VERSION,
            "type": "authenticated",
            "binding_id": binding_id,
            "expires_in": binding_store.BINDING_TTL_SECONDS,
        }))

    async def _ping(self):
        """Liveness, both ways — and the socket's only self-expiry path."""
        try:
            record = await sync_to_async(binding_store.get)(self.binding_id)
        except binding_store.StoreUnavailable:
            await self.close(code=CLOSE_DEPENDENCY_UNAVAILABLE)
            return
        if record is None:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return
        await self.send(text_data=json.dumps({
            "v": events.ENVELOPE_VERSION,
            "type": "pong",
            "server_time": timezone.now().isoformat(),
        }))

    # ── outbound ──────────────────────────────────────────────────────────

    async def collaboration_event(self, message):
        """Deliver one ``events.publish`` envelope verbatim.

        No filtering here: ``publish`` already decided who may receive this by
        choosing the group. Re-deriving the audience at delivery time would be
        a second, divergent copy of the authorization rule.
        """
        envelope = message.get("envelope")
        if envelope is None:
            return
        await self.send(text_data=json.dumps(envelope))
