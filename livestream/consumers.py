# isort: skip_file
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import json
import redis
from django.utils import timezone

from livestream.services.session_state import get_session_state, set_session_state
from .models import LiveSession
from enrollments.models import Enrollment

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

    def get_chat_history(self):
        try:
            key = f"chat:{self.session_id}"
            messages = r.lrange(key, 0, 99)
            return [json.loads(m) for m in messages]
        except Exception as e:
            print(f"get_chat_history error: {e}")
            return []

    def save_chat_message(self, message):
        try:
            key = f"chat:{self.session_id}"
            r.rpush(key, json.dumps(message))
            r.expire(key, 86400)  # 24 hours
        except Exception as e:
            print(f"save_chat_message error: {e}")

    @database_sync_to_async
    def get_user_name(self, user):
        try:
            profile = user.profile
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
