# PLACEMENT: backend/backend/config/asgi.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/config/asgi.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The forum websocket route is removed. ws/notifications/ (forum's
# NotificationConsumer on group notifications_<id>) and ws/updates/
# (accounts' UserUpdateConsumer on group user_updates_<id>) were two parallel
# per-user buses. Patch set 1 made ws/updates/ the canonical one — senders and
# the frontend hook now target it — so the forum socket is redundant.
# forum/consumers.py and forum/routing.py can be deleted (see patch README).
# Forum REST endpoints (api/forum/) are untouched.

# isort: skip_file
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
django_asgi_app = get_asgi_application()
import livestream.routing
import sessions_app.routing
from accounts.routing import websocket_urlpatterns as accounts_ws
from chat.routing import websocket_urlpatterns as chat_ws
from accounts.middleware import JWTAuthMiddleware

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": JWTAuthMiddleware(
        URLRouter(
            livestream.routing.websocket_urlpatterns
            + sessions_app.routing.websocket_urlpatterns
            + accounts_ws
            + chat_ws
        )
    ),
})
