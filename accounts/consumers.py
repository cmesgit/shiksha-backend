# PLACEMENT: backend/backend/accounts/consumers.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/accounts/consumers.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The group stays user_updates_<user_id> (zero producer changes, safe
# deploy), but the consumer now FILTERS each event against the identity
# of the connection before forwarding it. JWTAuthMiddleware already put
# the claims on the scope — they were simply never read:
#
#     scope["context"]            "learner" | "teacher" | "account"
#     scope["active_profile_id"]  LearnerProfile id or None
#
# Matching rules, applied to event["data"]:
#   • data.audience == "TEACHER"  → dropped on learner-context sockets
#   • data.audience == "LEARNER"  → dropped on teacher-context sockets
#   • data.learner_profile_id set → on learner sockets, dropped unless it
#     equals the connection's active profile
#   • no audience field           → pass-through (legacy producers:
#     sessions_app / materials / private-session pushes keep their
#     current account-wide behavior until they adopt the envelope)
#
# Net effect with the new activity/signals.py: a parent with two child
# profiles + a teacher identity can have three tabs open and each tab's
# bell only ever pulses for its own identity.

import json

from channels.generic.websocket import AsyncWebsocketConsumer


class UserUpdateConsumer(AsyncWebsocketConsumer):
    """Per-user event bus with per-connection identity filtering.

    Route:  ws/updates/            (accounts/routing.py — unchanged)
    Group:  user_updates_<user_id>

    Producers group_send() with either:
        {"type": "user_update",       "data": {...}}
        {"type": "send_notification", "data": {...}}
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            # 4401 mirrors the chat consumer's "unauthenticated" close code
            # so clients can refresh the token before reconnecting.
            await self.close(code=4401)
            return

        # Identity of THIS connection (set by JWTAuthMiddleware).
        self.ctx = self.scope.get("context")                      # learner|teacher|account
        pid = self.scope.get("active_profile_id")
        self.profile_id = str(pid) if pid else None

        self.group_name = f"user_updates_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Keepalive: clients ping every ~25s so proxies don't idle-close.
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
            return
        if payload.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    # ── identity gate ────────────────────────────────────────────────

    def _wanted(self, data):
        """Should THIS connection receive this event?"""
        if not isinstance(data, dict):
            return True  # legacy / opaque payloads: current behavior

        audience = data.get("audience")
        if audience == "TEACHER" and self.ctx != "teacher":
            return False
        if audience == "LEARNER" and self.ctx == "teacher":
            return False

        lp = data.get("learner_profile_id")
        if lp and self.ctx == "learner" and self.profile_id and str(lp) != self.profile_id:
            return False

        return True

    # ── group event handlers ─────────────────────────────────────────

    async def user_update(self, event):
        data = event.get("data")
        if not self._wanted(data):
            return
        await self.send(text_data=json.dumps({
            "type": "user_update",
            "data": data,
        }))

    async def send_notification(self, event):
        data = event.get("data")
        if not self._wanted(data):
            return
        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": data,
        }))
