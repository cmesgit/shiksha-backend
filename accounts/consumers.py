# PLACEMENT: backend/backend/accounts/consumers.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/accounts/consumers.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# This consumer (route: ws/updates/, group: user_updates_<user_id>) is now the
# ONE canonical per-user event bus. Previously the notification producers
# (livestream/services/notifications.py + livestream/tasks.py) broadcast to a
# group named `notifications_<id>` with event type `send_notification` — a
# group NO consumer ever joined, so every push was silently dropped.
#
# Fixes here:
#   • Handles BOTH event types: "user_update" and "send_notification", so the
#     producers (patched to target user_updates_<id>) and any legacy callers
#     both land.
#   • Frames every outbound message in a typed envelope:
#         {"type": "notification", "data": {...}}   ← send_notification events
#         {"type": "user_update",  "data": {...}}   ← user_update events
#     so the frontend can switch on `type` instead of guessing.
#
# NOTE: nothing previously sent `user_update` events (the group had no
# producers at all), so the new envelope breaks no existing client.

import json
from channels.generic.websocket import AsyncWebsocketConsumer


class UserUpdateConsumer(AsyncWebsocketConsumer):
    """Per-user event bus.

    Route:  ws/updates/            (accounts/routing.py — unchanged)
    Group:  user_updates_<user_id>

    Producers group_send() with either:
        {"type": "user_update",       "data": {...}}
        {"type": "send_notification", "data": {...}}
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            # 4401 mirrors the chat consumer's "unauthenticated" close code so
            # clients can distinguish auth failures from network drops and
            # trigger a token refresh before reconnecting.
            await self.close(code=4401)
            return
        self.group_name = f"user_updates_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Keepalive: clients ping every ~25s so proxies don't idle-close the
        # socket. Answer pings; ignore everything else (this bus is read-only).
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
            return
        if payload.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    # ── group event handlers ────────────────────────────────────────────

    async def user_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "user_update",
            "data": event.get("data"),
        }))

    async def send_notification(self, event):
        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": event.get("data"),
        }))
