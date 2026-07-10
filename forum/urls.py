# PLACEMENT: backend/backend/forum/urls.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/forum/urls.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The three notification paths are kept at the SAME URLs but now point at
# the Legacy* views in the notifications app (same response shapes), so
# the deployed dashboards keep working with zero frontend edits. Once
# every bell calls /api/notifications/ instead, delete those three paths
# and the Legacy* views.

from django.urls import path
from .views import (
    ListTagsView,
    ListThreadsView,
    CreateThreadView,
    ThreadDetailView,
    DeleteThreadView,
    ListCommentsView,
    CreateCommentView,
    DeleteCommentView,
    TogglePostUpvoteView,
    ToggleCommentUpvoteView,
    AcceptAnswerView,
    PublicForumProfileView,
    UpdateForumProfileView,
    ListUserRepliesView,
)
from notifications.views import (
    LegacyListNotificationsView,
    LegacyMarkAllNotificationsReadView,
    LegacyMarkNotificationReadView,
)

urlpatterns = [
    # Tags
    path("tags/", ListTagsView.as_view(), name="forum-tags"),

    # Threads
    path("threads/", ListThreadsView.as_view(), name="forum-threads"),
    path("threads/create/", CreateThreadView.as_view(), name="forum-create-thread"),
    path("threads/<int:thread_id>/", ThreadDetailView.as_view(), name="forum-thread-detail"),
    path("threads/<int:thread_id>/delete/", DeleteThreadView.as_view(), name="forum-delete-thread"),
    path("threads/<int:thread_id>/accept/<int:reply_id>/", AcceptAnswerView.as_view(), name="forum-accept-answer"),

    # Comments
    path("threads/<int:thread_id>/comments/", ListCommentsView.as_view(), name="forum-comments"),
    path("threads/<int:thread_id>/comments/create/", CreateCommentView.as_view(), name="forum-create-comment"),
    path("comments/<int:comment_id>/delete/", DeleteCommentView.as_view(), name="forum-delete-comment"),

    # Upvotes
    path("threads/<int:thread_id>/upvote/", TogglePostUpvoteView.as_view(), name="forum-toggle-post-upvote"),
    path("comments/<int:comment_id>/upvote/", ToggleCommentUpvoteView.as_view(), name="forum-toggle-comment-upvote"),

    # Public profile
    path("users/<str:username>/", PublicForumProfileView.as_view(), name="forum-public-profile"),
    path("users/<str:username>/replies/", ListUserRepliesView.as_view(), name="forum-user-replies"),
    path("profile/", UpdateForumProfileView.as_view(), name="forum-update-profile"),

    # Notifications — LEGACY ALIASES (same URLs, same shapes, new table).
    path("notifications/", LegacyListNotificationsView.as_view(), name="forum-notifications"),
    path("notifications/read/", LegacyMarkAllNotificationsReadView.as_view(), name="forum-mark-all-read"),
    path("notifications/<int:notification_id>/read/", LegacyMarkNotificationReadView.as_view(), name="forum-mark-read"),
]
