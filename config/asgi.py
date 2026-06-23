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
