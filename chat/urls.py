from django.urls import path
from .views import (
    ConversationListView,
    StartDirectView,
    CourseRoomView,
    MessageListView,
    MarkReadView,
)

urlpatterns = [
    path("conversations/", ConversationListView.as_view()),
    path("conversations/direct/", StartDirectView.as_view()),
    path("conversations/course/", CourseRoomView.as_view()),
    path("conversations/<uuid:conversation_id>/messages/", MessageListView.as_view()),
    path("conversations/<uuid:conversation_id>/read/", MarkReadView.as_view()),
]
