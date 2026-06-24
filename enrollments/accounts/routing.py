from django.urls import re_path
from .consumers import UserUpdateConsumer

websocket_urlpatterns = [
    re_path(r'ws/updates/$', UserUpdateConsumer.as_asgi()),
]
