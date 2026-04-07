import json
import asyncio
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

# How long (seconds) to wait after everyone leaves before auto-ending the session
AUTO_EXPIRE_DELAY = 5 * 60  # 5 minutes

# In-memory map of pending auto-expire asyncio tasks keyed by session_id.
# Used to cancel the timer if someone rejoins before it fires.
_expire_tasks: dict[str, asyncio.Task] = {}


class PrivateSessionChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for private session real-time chat.

    Also tracks active connections per session so rooms can auto-expire
    when every participant has left for 5 minutes.
    """

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"private_session_chat_{self.session_id}"

        # Join the channel-layer group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # ── Track this connection ──────────────────────────────────
        await self._increment_connections()

        # If an auto-expire timer is pending for this room, cancel it
        task = _expire_tasks.pop(self.session_id, None)
        if task and not task.done():
            task.cancel()
            logger.info(
                "Auto-expire timer cancelled for session %s (participant rejoined)",
                self.session_id,
            )

    async def disconnect(self, close_code):
        # Leave the channel-layer group
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

        # ── Track this disconnection ───────────────────────────────
        remaining = await self._decrement_connections()

        if remaining <= 0:
            # Everyone has left — start the 5-minute countdown
            await self._mark_all_left()
            logger.info(
                "All participants left session %s — starting %ds auto-expire timer",
                self.session_id,
                AUTO_EXPIRE_DELAY,
            )
            # Schedule the auto-expire check (non-blocking)
            task = asyncio.ensure_future(
                self._auto_expire_after_delay(self.session_id)
            )
            _expire_tasks[self.session_id] = task

    async def chat_message(self, event):
        """Receives broadcast from views.send_chat_message and forwards to WebSocket client."""
        await self.send(text_data=json.dumps({
            "type": "chat_message",
            "data": event["data"],
        }))

    # ──────────────────────────────────────────────────────────────
    # Connection-count helpers (DB operations via sync_to_async)
    # ──────────────────────────────────────────────────────────────

    @database_sync_to_async
    def _increment_connections(self):
        """Atomically add 1 to active_connections and clear all_left_at."""
        from .models import PrivateSession

        PrivateSession.objects.filter(
            pk=self.session_id, status="ongoing"
        ).update(
            active_connections=F("active_connections") + 1,
            all_left_at=None,
        )

    @database_sync_to_async
    def _decrement_connections(self) -> int:
        """
        Atomically subtract 1 from active_connections.
        Returns the new count (clamped to 0).
        """
        from .models import PrivateSession

        PrivateSession.objects.filter(
            pk=self.session_id, status="ongoing"
        ).update(
            active_connections=F("active_connections") - 1,
        )

        # Read back the current value
        try:
            session = PrivateSession.objects.get(pk=self.session_id)
            count = max(session.active_connections, 0)
            # Clamp to 0 if it somehow went negative
            if session.active_connections < 0:
                session.active_connections = 0
                session.save(update_fields=["active_connections"])
            return count
        except PrivateSession.DoesNotExist:
            return 0

    @database_sync_to_async
    def _mark_all_left(self):
        """Set all_left_at timestamp when the room becomes empty."""
        from .models import PrivateSession

        PrivateSession.objects.filter(
            pk=self.session_id, status="ongoing"
        ).update(
            all_left_at=timezone.now(),
            active_connections=0,  # ensure clean state
        )

    # ──────────────────────────────────────────────────────────────
    # Auto-expire logic
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _auto_expire_after_delay(session_id: str):
        """
        Wait AUTO_EXPIRE_DELAY seconds, then check if the room is still
        empty. If so, end the session automatically.
        """
        try:
            await asyncio.sleep(AUTO_EXPIRE_DELAY)
        except asyncio.CancelledError:
            # Timer was cancelled because someone rejoined
            return

        # Remove ourselves from the pending-tasks map
        _expire_tasks.pop(session_id, None)

        # Check DB and end if still empty
        ended = await PrivateSessionChatConsumer._try_auto_end(session_id)
        if ended:
            logger.info(
                "Session %s auto-expired after %ds with no participants",
                session_id,
                AUTO_EXPIRE_DELAY,
            )

    @staticmethod
    @database_sync_to_async
    def _try_auto_end(session_id: str) -> bool:
        """
        If the session is still ongoing AND all_left_at is set (meaning
        nobody reconnected), end it. Returns True if ended.
        """
        from .models import PrivateSession

        try:
            session = PrivateSession.objects.get(pk=session_id)
        except PrivateSession.DoesNotExist:
            return False

        # Only auto-end if still ongoing and still empty
        if session.status != "ongoing":
            return False
        if session.all_left_at is None:
            return False  # someone reconnected
        if session.active_connections > 0:
            return False  # someone is still here

        # Use the shared helper from views
        from .views import _end_session_internal
        return _end_session_internal(session, reason="auto_expired_all_left")
