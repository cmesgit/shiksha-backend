from django.urls import re_path
from .consumers import PrivateSessionChatConsumer, UserNotificationConsumer

websocket_urlpatterns = [
    # Per-session chat + connection tracking
    re_path(
        r"ws/private-session/(?P<session_id>[^/]+)/chat/$",
        PrivateSessionChatConsumer.as_asgi(),
    ),
    # Per-user session status notifications (no session_id needed)
    re_path(
        r"ws/private-session/notify/$",
        UserNotificationConsumer.as_asgi(),
    ),
]
