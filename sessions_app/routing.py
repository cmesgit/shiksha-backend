from django.urls import re_path
from .consumers import PrivateSessionChatConsumer

websocket_urlpatterns = [
    re_path(
        r"ws/private-session/(?P<session_id>[^/]+)/chat/$",
        PrivateSessionChatConsumer.as_asgi(),
    ),
]
