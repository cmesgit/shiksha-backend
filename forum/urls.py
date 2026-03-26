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
)

urlpatterns = [
    # Tags
    path("tags/", ListTagsView.as_view(), name="forum-tags"),

    # Threads
    path("threads/", ListThreadsView.as_view(), name="forum-threads"),
    path("threads/create/", CreateThreadView.as_view(), name="forum-create-thread"),
    path("threads/<int:thread_id>/", ThreadDetailView.as_view(), name="forum-thread-detail"),
    path("threads/<int:thread_id>/delete/", DeleteThreadView.as_view(), name="forum-delete-thread"),

    # Comments
    path("threads/<int:thread_id>/comments/", ListCommentsView.as_view(), name="forum-comments"),
    path("threads/<int:thread_id>/comments/create/", CreateCommentView.as_view(), name="forum-create-comment"),
    path("comments/<int:comment_id>/delete/", DeleteCommentView.as_view(), name="forum-delete-comment"),

    # Upvotes
    path("threads/<int:thread_id>/upvote/", TogglePostUpvoteView.as_view(), name="forum-toggle-post-upvote"),
    path("comments/<int:comment_id>/upvote/", ToggleCommentUpvoteView.as_view(), name="forum-toggle-comment-upvote"),
]
