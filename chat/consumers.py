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
  { "type": "message", "body": "...", "client_id": "..." }
  { "type": "typing" }
  { "type": "read" }

Server → client:
  { "type": "history", "data": [ ...messages ] }
  { "type": "message", "data": { ...message } }
  { "type": "typing",  "data": { "identity": "L:...", "name": "..." } }
  { "type": "error",   "data": { "category": "policy"|"profanity"|"political"
                                 |"blocked"|"rate_limited",
                                 "reason": "...", "client_id": "..." } }

The "error" frame is sent ONLY back to the sender (never broadcast). It fires
when the M3 structural policy gate refuses the conversation itself (frozen /
read-only broadcast — category "policy"), when content moderation rejects
the text, when a block exists in either direction on a direct thread, or
when the sender has hit the M0 rate limit (chat/redis_utils.py — 10 msgs/10s
burst, 200/hour). Policy, blocking, and rate limits are all re-evaluated on
every send, so they take effect immediately for an open socket.

Close codes: 4401 unauthenticated, 4403 not a participant, 4429 too many
connection attempts (redis_utils.check_connect_rate_limit — 20/min/account).
"""
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import Conversation, Participant
from . import services
from . import redis_utils


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

        self.group_name = f"chat_{self.conversation_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        history = await database_sync_to_async(self._history)()
        await self.send(text_data=json.dumps({"type": "history", "data": history}))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
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
                payload.get("body", ""), client_id
            )
            if error:
                # Refused (moderation or block) — inform the sender only.
                error = dict(error)
                error["client_id"] = client_id
                await self.send(text_data=json.dumps({"type": "error", "data": error}))
            elif msg:
                await self.channel_layer.group_send(
                    self.group_name,
                    {"type": "chat.message", "data": services.serialize_message(msg)},
                )
        elif mtype == "typing":
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "chat.typing",
                 "data": {"identity": self.me.identity_key(),
                          "name": self.me.display_name()}},
            )
        elif mtype == "read":
            await database_sync_to_async(self._mark_read)()

    # --- group event handlers ---
    async def chat_message(self, event):
        await self.send(text_data=json.dumps({"type": "message", "data": event["data"]}))

    async def chat_typing(self, event):
        # Don't echo typing back to the sender.
        if event["data"].get("identity") == self.me.identity_key():
            return
        await self.send(text_data=json.dumps({"type": "typing", "data": event["data"]}))

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
        return services.participant_for(conv, kind, obj)

    def _history(self):
        conv = self._resolve_conversation()
        msgs = list(conv.messages.order_by("-created_at")[:50])[::-1]
        return [services.serialize_message(m) for m in msgs]

    def _post_checked(self, body, client_id):
        """Funnels through the single moderation + block gate.
        Returns (message_or_None, error_or_None)."""
        conv = self._resolve_conversation()
        return services.post_message_checked(conv, self.me, body, client_id)

    def _mark_read(self):
        from django.utils import timezone
        self.me.last_read_at = timezone.now()
        self.me.save(update_fields=["last_read_at"])
        redis_utils.clear_unread(self.me.identity_key(), self.conversation_id)
