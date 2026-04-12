from django.urls import re_path
from .consumers import LiveSessionConsumer, CourseSessionConsumer

websocket_urlpatterns = [
    re_path(r"ws/live-session/(?P<session_id>[^/]+)/$", LiveSessionConsumer.as_asgi()),
    re_path(r"ws/course-sessions/(?P<course_id>[^/]+)/$", CourseSessionConsumer.as_asgi()),
]
