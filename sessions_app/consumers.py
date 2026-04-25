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


# ===========================================================================
# Study Group consumer — mirrors PrivateSessionChatConsumer but writes to
# a different table and uses a 7-minute idle grace window.
# ===========================================================================

STUDY_GROUP_AUTO_EXPIRE_DELAY = 7 * 60  # 7 minutes
_sg_expire_tasks: dict[str, asyncio.Task] = {}


class StudyGroupPresenceConsumer(AsyncWebsocketConsumer):
    """
    Tracks active connections for a StudyGroupSession room so the room
    can auto-expire after 7 minutes of emptiness.  Also relays simple
    presence events to other participants (so clients can refresh
    participant counts without polling).
    """

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"study_group_presence_{self.session_id}"

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self._increment_connections()

        task = _sg_expire_tasks.pop(self.session_id, None)
        if task and not task.done():
            task.cancel()
            logger.info(
                "Study-group auto-expire timer cancelled for %s (rejoin)",
                self.session_id,
            )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

        remaining = await self._decrement_connections()
        if remaining <= 0:
            await self._mark_all_left()
            logger.info(
                "All participants left study group %s — starting %ds timer",
                self.session_id, STUDY_GROUP_AUTO_EXPIRE_DELAY,
            )
            task = asyncio.ensure_future(
                self._auto_expire_after_delay(self.session_id)
            )
            _sg_expire_tasks[self.session_id] = task

    # ── DB helpers ───────────────────────────────────────────────────
    @database_sync_to_async
    def _increment_connections(self):
        from .models import StudyGroupSession
        StudyGroupSession.objects.filter(
            pk=self.session_id, status="live"
        ).update(
            active_connections=F("active_connections") + 1,
            all_left_at=None,
        )

    @database_sync_to_async
    def _decrement_connections(self) -> int:
        from .models import StudyGroupSession
        StudyGroupSession.objects.filter(
            pk=self.session_id, status="live"
        ).update(
            active_connections=F("active_connections") - 1,
        )
        try:
            session = StudyGroupSession.objects.get(pk=self.session_id)
            count = max(session.active_connections, 0)
            if session.active_connections < 0:
                session.active_connections = 0
                session.save(update_fields=["active_connections"])
            return count
        except StudyGroupSession.DoesNotExist:
            return 0

    @database_sync_to_async
    def _mark_all_left(self):
        from .models import StudyGroupSession
        StudyGroupSession.objects.filter(
            pk=self.session_id, status="live"
        ).update(
            all_left_at=timezone.now(),
            active_connections=0,
        )

    # ── Auto-expire ──────────────────────────────────────────────────
    @staticmethod
    async def _auto_expire_after_delay(session_id: str):
        try:
            await asyncio.sleep(STUDY_GROUP_AUTO_EXPIRE_DELAY)
        except asyncio.CancelledError:
            return

        _sg_expire_tasks.pop(session_id, None)
        ended = await StudyGroupPresenceConsumer._try_auto_end(session_id)
        if ended:
            logger.info(
                "Study group %s auto-expired after %ds with no participants",
                session_id, STUDY_GROUP_AUTO_EXPIRE_DELAY,
            )

    @staticmethod
    @database_sync_to_async
    def _try_auto_end(session_id: str) -> bool:
        from .models import StudyGroupSession
        try:
            session = StudyGroupSession.objects.get(pk=session_id)
        except StudyGroupSession.DoesNotExist:
            return False
        if session.status != "live":
            return False
        if session.all_left_at is None:
            return False
        if session.active_connections > 0:
            return False
        from .study_group_views import _end_study_group_internal
        return _end_study_group_internal(session, reason="auto_expired_all_left")


class UserNotificationConsumer(AsyncWebsocketConsumer):
    """
    Per-user WebSocket consumer.
    Each authenticated user connects to ws/private-session/notify/
    and joins their personal group user_<user_id>.

    Receives session_update events broadcast by views whenever a session
    status changes, and forwards them to the connected client.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.user_id = str(user.id)
        self.group_name = f"user_{self.user_id}"

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info(
            "UserNotificationConsumer connected for user %s", self.user_id)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def session_update(self, event):
        """Receives broadcast from _broadcast_session_update in views.py."""
        await self.send(text_data=json.dumps({
            "type": "session_update",
            "data": event["data"],
        }))
