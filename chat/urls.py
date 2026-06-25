# PLACEMENT: backend/backend/chat/urls.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/urls.py
from django.urls import path
from .views import (
    ConversationListView,
    StartDirectView,
    CourseRoomView,
    MessageListView,
    MarkReadView,
    DirectoryView,
    BlockListView,
    UnblockView,
)

urlpatterns = [
    path("conversations/", ConversationListView.as_view()),
    path("conversations/direct/", StartDirectView.as_view()),
    path("conversations/course/", CourseRoomView.as_view()),
    path("conversations/<uuid:conversation_id>/messages/", MessageListView.as_view()),
    path("conversations/<uuid:conversation_id>/read/", MarkReadView.as_view()),

    # NEW — start-a-new-chat directory + blocking
    path("directory/", DirectoryView.as_view()),
    path("blocks/", BlockListView.as_view()),
    path("blocks/remove/", UnblockView.as_view()),
]
