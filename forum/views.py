# PLACEMENT: backend/backend/forum/views.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/forum/views.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# 1. NOTIFICATIONS EXTRACTED: forum no longer owns a Notification model.
#    CreateCommentView now calls notifications.services.notify(), which
#    persists the row in the site-wide table AND pushes over the existing
#    user_updates_<id> websocket bus in one call. The ws_extra kwarg keeps
#    emitting the legacy frame keys (type/notification_type/message/
#    thread_id/title) so the deployed NotificationBell components keep
#    rendering pushes without any frontend edit.
# 2. The three notification API views (list / mark-all / mark-one) moved to
#    notifications/views.py. forum/urls.py still exposes the OLD paths,
#    pointing at the new Legacy* views — response shapes are identical.
# 3. Everything else (moderation gate, distinct=True counts, pagination
#    guards, Exists() annotations, staff comment deletion) is unchanged.
#
# API shapes are unchanged; the frontend needs no edits.

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Exists, OuterRef

from .models import Tag, ForumPost, Reply, PostUpvote, ReplyUpvote
from .serializers import (
    TagSerializer,
    ForumPostSerializer,
    CreateThreadSerializer,
    CommentSerializer,
    CreateCommentSerializer,
)
from django.contrib.auth import get_user_model
from notifications.services import notify
from chat import moderation

User = get_user_model()


# =====================================================
# Helpers
# =====================================================

def _int_param(request, name, default, maximum):
    """Parse ?name= as a positive int; garbage falls back to the default."""
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(1, value), maximum)


def _moderation_error(text):
    """Run the shared chat moderation gate. Returns a Response to send back,
    or None if the text is clean."""
    verdict = moderation.check_message(text or "")
    if verdict.ok:
        return None
    return Response(
        {"category": verdict.category, "reason": verdict.reason},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _annotated_threads(user):
    qs = ForumPost.objects.select_related("author").prefetch_related("tags").annotate(
        reply_count=Count("replies", distinct=True),
        upvote_count=Count("upvotes", distinct=True),
    )
    if user and user.is_authenticated:
        qs = qs.annotate(
            user_has_upvoted_annotated=Exists(
                PostUpvote.objects.filter(post=OuterRef("pk"), user=user)
            )
        )
    return qs


# =====================================================
# Tag Views
# =====================================================
class ListTagsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        tags = Tag.objects.all().order_by("name")
        serializer = TagSerializer(tags, many=True)
        return Response(serializer.data)


# =====================================================
# Thread (ForumPost) Views
# =====================================================
class ListThreadsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = _annotated_threads(request.user)

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(title__icontains=search) |
                           Q(content__icontains=search))

        tag = request.query_params.get("tag")
        if tag:
            qs = qs.filter(tags__name=tag)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        sort = request.query_params.get("sort", "newest")
        if sort == "oldest":
            qs = qs.order_by("created_at")
        elif sort == "popular":
            qs = qs.order_by("-upvote_count", "-created_at")
        else:
            qs = qs.order_by("-created_at")

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        threads = qs[start:start + page_size]

        serializer = ForumPostSerializer(
            threads, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": total})


class CreateThreadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreateThreadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        title = serializer.validated_data["title"]
        body = serializer.validated_data.get("body", "")

        # Same gate the chat uses — a school forum should not be looser
        # than the school chat.
        blocked = _moderation_error(f"{title}\n{body}")
        if blocked is not None:
            return blocked

        post = ForumPost.objects.create(
            author=request.user,
            title=title,
            content=body,
        )

        tag_names = serializer.validated_data.get("tags", [])
        for name in tag_names:
            clean = name.lower().strip()
            if clean:
                tag, _ = Tag.objects.get_or_create(name=clean)
                post.tags.add(tag)

        # NOTE: the old notify-EVERY-user fan-out (N rows + N Celery tasks per
        # thread) is intentionally gone. Threads are discoverable in the list;
        # people are notified when someone engages with THEM (replies below).

        post = _annotated_threads(request.user).get(pk=post.pk)
        return Response(
            ForumPostSerializer(post, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ThreadDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, thread_id):
        post = get_object_or_404(_annotated_threads(request.user), pk=thread_id)
        return Response(ForumPostSerializer(post, context={"request": request}).data)


class DeleteThreadView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, thread_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        if post.author != request.user and not request.user.is_staff:
            return Response(
                {"detail": "You can only delete your own threads."},
                status=status.HTTP_403_FORBIDDEN,
            )
        post.delete()
        return Response(
            {"detail": "Thread deleted successfully."},
            status=status.HTTP_204_NO_CONTENT,
        )


# =====================================================
# Comment (Reply) Views
# =====================================================
class ListCommentsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, thread_id):
        get_object_or_404(ForumPost, pk=thread_id)
        qs = Reply.objects.filter(post_id=thread_id).select_related("author").annotate(
            upvote_count=Count("upvotes", distinct=True),
        )
        if request.user.is_authenticated:
            qs = qs.annotate(
                user_has_upvoted_annotated=Exists(
                    ReplyUpvote.objects.filter(reply=OuterRef("pk"), user=request.user)
                )
            )

        sort = request.query_params.get("sort", "oldest")
        if sort == "newest":
            qs = qs.order_by("-created_at")
        else:
            qs = qs.order_by("created_at")

        total = qs.count()
        serializer = CommentSerializer(
            qs, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": total})


class CreateCommentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        serializer = CreateCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        content = serializer.validated_data["content"]
        blocked = _moderation_error(content)
        if blocked is not None:
            return blocked

        reply_to_id = serializer.validated_data.get("reply_to_comment_id")
        reply_to = None
        if reply_to_id:
            reply_to = get_object_or_404(Reply, pk=reply_to_id, post=post)

        reply = Reply.objects.create(
            post=post,
            author=request.user,
            content=content,
            reply_to=reply_to,
        )

        # Notify the thread author…
        recipients = set()
        if post.author_id != request.user.id:
            recipients.add(post.author)
        # …and, on a nested reply, the parent comment's author.
        if reply_to and reply_to.author_id not in (request.user.id, post.author_id):
            recipients.add(reply_to.author)

        message = f'{request.user.username} replied to your thread: "{post.title}"'
        for recipient in recipients:
            # One call = persisted row + real-time WS push (user_updates_<id>).
            notify(
                recipient=recipient,
                actor=request.user,
                verb="forum.reply",
                title=message,
                body=message,
                link_url=f"/forum/thread/{post.id}",
                payload={
                    "thread_id": post.id,
                    "title": post.title,
                    "legacy_type": "new_reply",
                },
                # Legacy frame keys for the currently-deployed bells; drop
                # once the frontends read the canonical shape.
                ws_extra={
                    "type": "forum",
                    "notification_type": "new_reply",
                    "message": message,
                    "thread_id": str(post.id),
                },
            )

        reply = Reply.objects.filter(pk=reply.pk).annotate(
            upvote_count=Count("upvotes", distinct=True),
        ).select_related("author").first()

        return Response(
            CommentSerializer(reply, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class DeleteCommentView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, comment_id):
        reply = get_object_or_404(Reply, pk=comment_id)
        # Staff can moderate comments, matching thread deletion.
        if reply.author != request.user and not request.user.is_staff:
            return Response(
                {"detail": "You can only delete your own comments."},
                status=status.HTTP_403_FORBIDDEN,
            )
        reply.delete()
        return Response(
            {"detail": "Comment deleted successfully."},
            status=status.HTTP_204_NO_CONTENT,
        )


# =====================================================
# Upvote Views
# =====================================================
class TogglePostUpvoteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        upvote, created = PostUpvote.objects.get_or_create(
            user=request.user, post=post
        )
        if not created:
            upvote.delete()
            return Response({"upvoted": False, "upvote_count": post.upvotes.count()})
        return Response({"upvoted": True, "upvote_count": post.upvotes.count()})


class ToggleCommentUpvoteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, comment_id):
        reply = get_object_or_404(Reply, pk=comment_id)
        upvote, created = ReplyUpvote.objects.get_or_create(
            user=request.user, reply=reply
        )
        if not created:
            upvote.delete()
            return Response({"upvoted": False, "upvote_count": reply.upvotes.count()})
        return Response({"upvoted": True, "upvote_count": reply.upvotes.count()})
