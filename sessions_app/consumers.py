"""
WebSocket consumer for private session chat.
Handles real-time message delivery for persistent chat.
"""

from channels.generic.websocket import AsyncWebsocketConsumer
import json


class PrivateSessionChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"private_session_chat_{self.session_id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name,
        )

    async def chat_message(self, event):
        """Broadcast chat message to connected WebSocket clients."""
        await self.send(text_data=json.dumps({
            "type": "chat_message",
            "data": event["data"],
        }))
