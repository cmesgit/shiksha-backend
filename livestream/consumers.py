# isort: skip_file
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import json
import logging
import redis
from django.utils import timezone

from livestream.services.session_state import get_session_state, set_session_state
from .models import LiveSession, LiveSessionChatMessage
from enrollments.models import Enrollment

logger = logging.getLogger(__name__)
r = redis.Redis(host="127.0.0.1", port=6379, db=0)


class LiveSessionConsumer(AsyncWebsocketConsumer):
    """
    Handles the in-session WebSocket connection.
    Serves: chat history, chat messages, session state updates.
    """

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"session_{self.session_id}"
        self.user = self.scope["user"]

        # Auth gate — reject anonymous and anyone not entitled to this session
        # (mirrors CourseSessionConsumer; the previous version accepted anyone,
        # so any presence-based metric built on this consumer was untrusted).
        if getattr(self.user, "is_anonymous", True):
            await self.close()
            return

        authorized = await database_sync_to_async(self._is_authorized)()
        if not authorized:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send chat history (wrapped in sync_to_async — Redis is blocking)
        chat_history = await database_sync_to_async(self.get_chat_history)()
        if chat_history:
            await self.send(text_data=json.dumps({
                "type": "chat_history",
                "data": chat_history
            }))

        # Get session state from Redis
        state = await database_sync_to_async(get_session_state)(self.session_id)

        # Fallback to DB if Redis has no state
        if not state:
            try:
                session = await database_sync_to_async(
                    LiveSession.objects.get
                )(id=self.session_id)

                state = {
                    "status": session.computed_status(),
                    "teacher_left_at": (
                        session.teacher_left_at.isoformat()
                        if session.teacher_left_at else None
                    ),
                }

                await database_sync_to_async(set_session_state)(session)
            except LiveSession.DoesNotExist:
                await self.close()
                return

        # Send initial state so client knows current status immediately
        await self.send(text_data=json.dumps({
            "type": "initial_state",
            "data": state
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type")
            if msg_type == "chat_message":
                await self.handle_chat(data)
        except json.JSONDecodeError:
            pass

    async def handle_chat(self, data):
        user = self.scope["user"]
        if user.is_anonymous:
            return

        sender_name = await self.get_user_name(user)
        role = await self.get_user_role(user)

        message_data = {
            "sender": sender_name,
            "text": data.get("text", ""),
            "role": role,
            "isTeacher": role == "TEACHER",
            "time": timezone.now().isoformat(),
            "sender_id": str(user.id),
        }

        # Save to Redis (wrapped — blocking call)
        await database_sync_to_async(self.save_chat_message)(message_data)

        # Broadcast to everyone in the session group
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat_message",
                "data": message_data
            }
        )

    def _is_authorized(self):
        """Teacher of the session's subject, or a user with an active enrollment
        in the course. Admins/staff are always allowed."""
        try:
            session = LiveSession.objects.select_related("subject", "course").get(
                id=self.session_id
            )
        except LiveSession.DoesNotExist:
            return False

        user = self.user
        if getattr(user, "is_staff", False):
            return True

        # Session creator / assigned teacher
        if str(session.created_by_id) == str(user.id):
            return True
        try:
            from courses.services import teaches_subject
            if user.has_role("TEACHER") and teaches_subject(user, session.subject):
                return True
        except Exception:
            pass

        # Active enrollment in the course
        return Enrollment.objects.filter(
            user=user, course_id=session.course_id, status=Enrollment.STATUS_ACTIVE
        ).exists()

    def get_chat_history(self):
        # Redis fast-path (last 100, 24h TTL).
        try:
            key = f"chat:{self.session_id}"
            messages = r.lrange(key, 0, 99)
            if messages:
                return [json.loads(m) for m in messages]
        except Exception:
            logger.warning("get_chat_history redis error", exc_info=True)

        # Fallback to the durable DB rows when Redis is empty/expired, so
        # history survives past the 24h/100-message Redis window.
        try:
            rows = (
                LiveSessionChatMessage.objects.filter(session_id=self.session_id)
                .order_by("created_at")[:100]
            )
            return [
                {
                    "sender": m.sender_name,
                    "text": m.text,
                    "role": "TEACHER" if m.is_teacher else "STUDENT",
                    "isTeacher": m.is_teacher,
                    "time": m.created_at.isoformat(),
                    "sender_id": str(m.user_id) if m.user_id else None,
                }
                for m in rows
            ]
        except Exception:
            logger.warning("get_chat_history db error", exc_info=True)
            return []

    def save_chat_message(self, message):
        # Save to Redis (fast, for active sessions)
        try:
            key = f"chat:{self.session_id}"
            r.rpush(key, json.dumps(message))
            r.expire(key, 86400)
        except Exception:
            logger.warning("save_chat_message redis error", exc_info=True)
        # Also save to DB (persistent). Best-effort, but logged (not swallowed).
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            session = LiveSession.objects.get(id=self.session_id)
            user = User.objects.filter(id=message.get("sender_id")).first()
            LiveSessionChatMessage.objects.create(
                session=session,
                user=user,
                sender_name=message.get("sender", ""),
                text=message.get("text", ""),
                is_teacher=message.get("isTeacher", False),
            )
        except Exception:
            logger.error("save_chat_message db error", exc_info=True)

    @database_sync_to_async
    def get_user_name(self, user):
        try:
            profile = user.default_learner_profile()
            return profile.full_name or profile.first_name or user.email
        except Exception:
            return user.email

    @database_sync_to_async
    def get_user_role(self, user):
        try:
            from accounts.models import UserRole
            role = UserRole.objects.filter(
                user=user,
                is_primary=True,
                is_active=True
            ).select_related("role").first()
            return role.role.name if role else "STUDENT"
        except Exception:
            return "STUDENT"

    async def session_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "session_update",
            "data": event["data"]
        }))

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "chat_message",
            "data": event["data"]
        }))


class CourseSessionConsumer(AsyncWebsocketConsumer):
    """
    Handles the session list page WebSocket connection.
    Students connect here to get real-time session create/cancel/status updates
    without refreshing LiveSessions.jsx.
    """

    async def connect(self):
        self.course_id = self.scope["url_route"]["kwargs"]["course_id"]
        self.group_name = f"course_sessions_{self.course_id}"
        self.user = self.scope["user"]

        if self.user.is_anonymous:
            await self.close()
            return

        # Verify the user is enrolled in this course
        is_enrolled = await database_sync_to_async(
            lambda: Enrollment.objects.filter(
                user=self.user,
                course_id=self.course_id,
                status="ACTIVE"
            ).exists()
        )()

        if not is_enrolled:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def session_list_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "session_list_update",
            "data": event["data"]
        }))
