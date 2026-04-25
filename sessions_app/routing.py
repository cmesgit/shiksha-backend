from django.urls import re_path
from .consumers import (
    PrivateSessionChatConsumer,
    StudyGroupPresenceConsumer,
    UserNotificationConsumer,
)

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
    # Study-group presence + idle-expire tracking
    re_path(
        r"ws/study-group/(?P<session_id>[^/]+)/presence/$",
        StudyGroupPresenceConsumer.as_asgi(),
    ),
]
