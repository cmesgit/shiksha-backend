# PLACEMENT: backend/backend/chat/consumers.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/consumers.py
"""
chat/consumers.py

WebSocket consumer for a single conversation room. Mirrors the livestream
app's pattern (AsyncWebsocketConsumer + channel_layer groups + JWTAuthMiddleware
populating scope). Uses the same Channels/Redis layer already configured in
settings (CHANNEL_LAYERS).

URL:  ws/chat/<conversation_id>/

The acting identity is resolved from scope["context"] + scope["active_profile_id"]
(set by the patched accounts/middleware.py), or — for tokens minted after M1
(Phase 3 §7) — the single scope["identity"] claim directly. A connection is
only accepted if the identity is an actual participant of the conversation.

Client → server:
  { "type": "message", "body": "...", "client_id": "...", "reply_to": "<msg-id>"? }
  { "type": "typing" }
  { "type": "read" }
  { "type": "ping" }

Server → client:
  { "type": "history", "data": [ ...messages ] }
  { "type": "message", "data": { ...message } }
  { "type": "typing",  "data": { "identity": "L:...", "name": "..." } }
  { "type": "reaction", "data": { "message_id", "reactions", "actor", "emoji", "action" } }
  { "type": "message_deleted", "data": { "id": "<msg-id>" } }
  { "type": "read", "data": { "identity": "L:...", "last_read_at": "..." } }
  { "type": "presence", "data": { "identity": "L:...", "online": true|false } }
  { "type": "error",   "data": { "category": "policy"|"profanity"|"political"
                                 |"blocked"|"suspended"|"rate_limited",
                                 "reason": "...", "client_id": "..." } }

The "error" frame is sent ONLY back to the sender (never broadcast). It fires
when the M3 structural policy gate refuses the conversation itself (frozen /
read-only broadcast — category "policy"), when content moderation rejects
the text, when a block exists in either direction on a direct thread, when
the sender is chat-suspended (Stage D · CC-023 — category "suspended"), or
when the sender has hit the M0 rate limit (chat/redis_utils.py — 10 msgs/10s
burst, 200/hour). Policy, blocking, suspension, and rate limits are all
re-evaluated on every send, so they take effect immediately for an open
socket.

Close codes: 4401 unauthenticated, 4403 not a participant, 4429 too many
connection attempts (redis_utils.check_connect_rate_limit — 20/min/account).

REALTIME FAN-OUT OWNERSHIP (Stage B/C): a successful "message" send no
longer group_sends here — chat/services.py's _finalize_new_message() does
that centrally now (called from post_message()/post_attachment_checked()),
so a WS-originated text send and a REST-originated attachment upload fan out
identically through one code path. This consumer's job for "message" is
purely: rate-limit, call the service, and report an ERROR back to the
sender if refused — the success case has nothing left to do here.

reaction/delete/attachment MUTATIONS themselves are REST endpoints (see
chat/views.py) — this consumer only RELAYS the realtime events they trigger
(chat_reaction/chat_message_deleted below) to everyone else with the thread
open. This keeps exactly one mutation path per action regardless of origin,
and keeps this consumer's own responsibilities to: history, send, typing,
read-receipt fan-out, and presence.
"""
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import Conversation, Participant
from . import services
from . import redis_utils

# Stage B/C (payload-size note from the gap analysis): messages now carry
# attachments/reply previews/reactions, so the WS connect-time history dump
# is deliberately smaller than before. Anything further back is the REST
# MessageListView's job (`?before=&limit=`), which the frontend's
# infinite-scroll-up now drives — see src/shared/chatClient.js's
# ChatAPI.messages().
WS_HISTORY_SIZE = 30


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.user = self.scope.get("user")
        self.context = self.scope.get("context")
        self.active_profile_id = self.scope.get("active_profile_id")
        self.identity_claim = self.scope.get("identity")  # M1 — may be None for pre-M1 tokens

        if not self.user or self.user.is_anonymous:
            await self.close(code=4401)
            return

        # M0: connect-rate-limit (fail open — see redis_utils docstring).
        allowed = await database_sync_to_async(redis_utils.check_connect_rate_limit)(
            f"acct:{self.user.id}"
        )
        if not allowed:
            await self.close(code=4429)
            return

        self.me = await database_sync_to_async(self._resolve_participant)()
        if not self.me:
            # Not a participant of this conversation in the current identity.
            await self.close(code=4403)
            return

        if not await database_sync_to_async(self._membership_still_valid)():
            await self.close(code=4403)
            return

        self.group_name = f"chat_{self.conversation_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Stage E (CC-006/008/021 presence): mark online + tell anyone else
        # already in this thread. Best-effort (redis_utils fails open).
        my_identity = self.me.identity_key()
        await database_sync_to_async(redis_utils.touch_presence)(my_identity)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "chat.presence", "data": {"identity": my_identity, "online": True}},
        )

        history = await database_sync_to_async(self._history)()
        await self.send(text_data=json.dumps({"type": "history", "data": history}))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            my_identity = self.me.identity_key()
            await database_sync_to_async(redis_utils.mark_offline)(my_identity)
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "chat.presence", "data": {"identity": my_identity, "online": False}},
            )
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
            return

        # Re-check on every frame (not just at connect) — a course-context
        # socket can sit open for a long time, and access may have been
        # revoked (unenrollment, teaching-assignment ended) since it was
        # accepted. The 25s "ping" heartbeat means a stale socket is closed
        # within one heartbeat cycle even if the client sends nothing else.
        if not await database_sync_to_async(self._membership_still_valid)():
            await self.close(code=4403)
            return

        mtype = payload.get("type")

        if mtype == "message":
            client_id = payload.get("client_id", "")
            limit_reason = await database_sync_to_async(
                redis_utils.check_message_rate_limit
            )(self.me.identity_key())
            if limit_reason:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "data": {
                        "category": "rate_limited",
                        "reason": limit_reason,
                        "client_id": client_id,
                    },
                }))
                return
            msg, error = await database_sync_to_async(self._post_checked)(
                payload.get("body", ""), client_id, payload.get("reply_to"),
            )
            if error:
                # Refused (policy / moderation / block / suspension) —
                # inform the sender only. On success there is nothing left
                # to do here: services._finalize_new_message() already fanned
                # the message out to this group (see module docstring).
                error = dict(error)
                error["client_id"] = client_id
                await self.send(text_data=json.dumps({"type": "error", "data": error}))
        elif mtype == "typing":
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "chat.typing",
                 "data": {"identity": self.me.identity_key(),
                          "name": self.my_display_name}},
            )
        elif mtype == "read":
            last_read_at = await database_sync_to_async(self._mark_read)()
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "chat.read",
                 "data": {"identity": self.me.identity_key(), "last_read_at": last_read_at}},
            )
        elif mtype == "ping":
            # Heartbeat (chatClient.js sends this every 25s to keep the
            # socket open through idle-killing proxies) — piggy-back a
            # presence touch so an unclean disconnect (killed tab) still
            # naturally expires within PRESENCE_TTL_SECONDS instead of
            # showing "online" forever.
            await database_sync_to_async(redis_utils.touch_presence)(self.me.identity_key())

    # --- group event handlers ---
    async def chat_message(self, event):
        await self.send(text_data=json.dumps({"type": "message", "data": event["data"]}))

    async def chat_typing(self, event):
        # Don't echo typing back to the sender.
        if event["data"].get("identity") == self.me.identity_key():
            return
        await self.send(text_data=json.dumps({"type": "typing", "data": event["data"]}))

    async def chat_reaction(self, event):
        await self.send(text_data=json.dumps({"type": "reaction", "data": event["data"]}))

    async def chat_message_deleted(self, event):
        await self.send(text_data=json.dumps({"type": "message_deleted", "data": event["data"]}))

    async def chat_read(self, event):
        # Don't echo a reader's own read-receipt back to themselves — only
        # the OTHER side needs to know "they've read up to here".
        if event["data"].get("identity") == self.me.identity_key():
            return
        await self.send(text_data=json.dumps({"type": "read", "data": event["data"]}))

    async def chat_presence(self, event):
        if event["data"].get("identity") == self.me.identity_key():
            return
        await self.send(text_data=json.dumps({"type": "presence", "data": event["data"]}))

    # --- sync DB helpers ---
    def _resolve_conversation(self):
        return Conversation.objects.filter(id=self.conversation_id).first()

    def _resolve_participant(self):
        conv = self._resolve_conversation()
        if not conv:
            return None
        kind, obj = services.active_identity_from_claims(
            self.user, self.context, self.active_profile_id, self.identity_claim
        )
        if not kind:
            return None
        participant = services.participant_for(conv, kind, obj)
        if participant is not None:
            # Cache the display name NOW, synchronously (we're inside
            # database_sync_to_async here), so the async "typing" branch
            # below never needs to lazily touch participant.teacher_profile
            # / .learner_profile itself. Pre-existing latent bug, found via
            # this stage's own realtime integration tests (not previously
            # covered): Participant.display_name() can trigger a fresh
            # query for an uncached relation, and doing that directly from
            # an async consumer method raises Django's
            # SynchronousOnlyOperation — identity_key() is safe (it only
            # ever reads the already-loaded *_id columns), but display_name()
            # is not, so it's the one thing worth caching rather than
            # calling fresh from async code.
            self.my_display_name = participant.display_name()
        return participant

    def _membership_still_valid(self):
        """Re-derive course access rather than trusting self.me's mere
        existence — a stale Participant row (course access revoked after
        this identity joined) would otherwise keep a long-lived socket
        connected, and able to read/post, indefinitely."""
        conv = self._resolve_conversation()
        if not conv:
            return False
        return services.is_course_membership_still_valid(conv, self.me)

    def _history(self):
        conv = self._resolve_conversation()
        msgs = list(
            conv.messages
            .select_related("sender", "reply_to", "reply_to__sender", "attachment")
            .prefetch_related("reactions__participant")
            .order_by("-created_at")[:WS_HISTORY_SIZE]
        )[::-1]
        return [services.serialize_message(m) for m in msgs]

    def _post_checked(self, body, client_id, reply_to_id=None):
        """Funnels through the single moderation + block + suspension gate.
        Returns (message_or_None, error_or_None)."""
        conv = self._resolve_conversation()
        return services.post_message_checked(conv, self.me, body, client_id, reply_to_id=reply_to_id)

    def _mark_read(self):
        from django.utils import timezone
        self.me.last_read_at = timezone.now()
        self.me.save(update_fields=["last_read_at"])
        redis_utils.clear_unread(self.me.identity_key(), self.conversation_id)
        return self.me.last_read_at.isoformat()
