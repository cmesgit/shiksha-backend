"""
chat/consumers.py

WebSocket consumer for a single conversation room. Mirrors the livestream
app's pattern (AsyncWebsocketConsumer + channel_layer groups + JWTAuthMiddleware
populating scope). Uses the same Channels/Redis layer already configured in
settings (CHANNEL_LAYERS).

URL:  ws/chat/<conversation_id>/

The acting identity is resolved from scope["context"] + scope["active_profile_id"]
(set by the patched accounts/middleware.py). A connection is only accepted if
the identity is an actual participant of the conversation.

Client → server:
  { "type": "message", "body": "...", "client_id": "..." }
  { "type": "typing" }
  { "type": "read" }

Server → client:
  { "type": "history", "data": [ ...messages ] }
  { "type": "message", "data": { ...message } }
  { "type": "typing", "data": { "identity": "L:...", "name": "..." } }
"""
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import Conversation, Participant
from . import services


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.user = self.scope.get("user")
        self.context = self.scope.get("context")
        self.active_profile_id = self.scope.get("active_profile_id")

        if not self.user or self.user.is_anonymous:
            await self.close(code=4401)
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
            msg = await database_sync_to_async(self._save_message)(
                payload.get("body", ""), payload.get("client_id", "")
            )
            if msg:
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
            self.user, self.context, self.active_profile_id
        )
        if not kind:
            return None
        return services.participant_for(conv, kind, obj)

    def _history(self):
        conv = self._resolve_conversation()
        msgs = list(conv.messages.order_by("-created_at")[:50])[::-1]
        return [services.serialize_message(m) for m in msgs]

    def _save_message(self, body, client_id):
        conv = self._resolve_conversation()
        return services.post_message(conv, self.me, body, client_id)

    def _mark_read(self):
        from django.utils import timezone
        self.me.last_read_at = timezone.now()
        self.me.save(update_fields=["last_read_at"])
