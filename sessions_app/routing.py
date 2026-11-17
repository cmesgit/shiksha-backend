from django.urls import re_path
from .collaboration.consumers import CollaborationEventConsumer
from .consumers import (
    PrivateSessionChatConsumer,
    GroupSessionChatConsumer,
    UserNotificationConsumer,
)

websocket_urlpatterns = [
    # Per-session chat + connection tracking (private sessions)
    re_path(
        r"ws/private-session/(?P<session_id>[^/]+)/chat/$",
        PrivateSessionChatConsumer.as_asgi(),
    ),
    # Per-user session status notifications (no session_id needed)
    re_path(
        r"ws/private-session/notify/$",
        UserNotificationConsumer.as_asgi(),
    ),
    # Per-session chat + connection tracking (group sessions)
    # Path matches the front-end's chatConfig.wsPath:
    #   /ws/group-session/<session_id>/chat/
    re_path(
        r"ws/group-session/(?P<session_id>[^/]+)/chat/$",
        GroupSessionChatConsumer.as_asgi(),
    ),
    # Shiksha Remote — collaboration invalidation bus (RC-006b).
    #
    # Purely additive: no existing socket, path or consumer changes. It takes
    # no session_id in the URL on purpose — the binding behind the ticket
    # names the room, so an unauthenticated caller cannot address a room it
    # has no binding for, and nothing room-shaped leaks into access logs.
    #
    # 🔴 It carries NO input. Pointer and keyboard ride the LiveKit data
    # channel; this socket is pings and invalidation only.
    #
    # 🔴 It does its own exact-Origin check. JWTAuthMiddleware wraps this
    # stack with no OriginValidator, which is a pre-existing repo-wide
    # property — see the consumer's docstring before "fixing" it here.
    re_path(
        r"ws/collaboration/v1/$",
        CollaborationEventConsumer.as_asgi(),
    ),
]
