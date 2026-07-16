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
from django.db.models import Count, Q, Exists, OuterRef, F, CharField
from django.db.models.functions import Cast
from django.utils import timezone

from .models import (
    Tag, ForumPost, Reply, PostUpvote, ReplyUpvote, ForumProfile,
    Space, SavedPost, Follow, Report, Attachment, AutoRejectedSubmission,
)
from .serializers import (
    TagSerializer,
    ForumPostSerializer,
    CreateThreadSerializer,
    CommentSerializer,
    ReplySerializer,
    CreateCommentSerializer,
    PublicForumProfileSerializer,
    UpdateForumProfileSerializer,
    UserReplySerializer,
    SpaceSerializer,
    CreateSpaceSerializer,
    AttachmentSerializer,
    CreateReportSerializer,
)
from .constants import FORUM_TOPICS, FORUM_CATEGORIES, FORUM_CATEGORIES_BY_ID, FORUM_PALETTE
from .utils import author_badge
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from notifications.services import notify
from chat import moderation
from . import moderation as forum_moderation

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


def _ban_error(user):
    """A banned OR currently-suspended user cannot write to the forum at
    all. Returns a Response to send back, or None if the user is clear to
    proceed. Suspension lifts itself lazily here — no cron job needed."""
    profile, _ = ForumProfile.objects.get_or_create(user=user)
    if profile.is_banned:
        return Response(
            {"detail": "You have been banned from the forum."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if profile.suspended_until and profile.suspended_until > timezone.now():
        return Response(
            {"detail": f"Your forum access is suspended until {profile.suspended_until:%d %b %Y}."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _lock_error(post):
    """A locked thread accepts no new replies/comments."""
    if post.is_locked:
        return Response(
            {"detail": "This thread is locked and no longer accepting replies."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _queue_auto_rejected(author, kind, title, content, categories, tags=None, thread=None):
    """Persist a scanner-flagged submission instead of creating the real
    ForumPost/Reply. Returns the created AutoRejectedSubmission."""
    return AutoRejectedSubmission.objects.create(
        author=author, kind=kind, title=title or "", content=content or "",
        thread=thread, tags=",".join(tags or []), categories=categories,
    )


def _annotated_threads(user, include_removed=False):
    """The single base queryset every public listing/detail view builds
    on. Moderator-removed threads are hidden here by default so hiding a
    thread from the public site is a one-line change, not an audit across
    every call site — pass include_removed=True only from the moderator-
    only thread list (forum/moderation_views.py), which needs to see (and
    restore) removed threads."""
    qs = (
        ForumPost.objects
        .select_related("author", "author__forum_profile", "space")
        .prefetch_related("tags", "attachments", "author__identities")
    )
    if not include_removed:
        qs = qs.filter(is_removed=False)
    qs = (
        qs
        .annotate(
            reply_count=Count("replies", distinct=True),
            answer_count_annotated=Count(
                "replies", filter=Q(replies__kind=Reply.KIND_ANSWER), distinct=True),
            comment_count_annotated=Count(
                "replies", filter=Q(replies__kind=Reply.KIND_COMMENT), distinct=True),
            upvote_count=Count("upvotes", distinct=True),
        )
    )
    if user and user.is_authenticated:
        qs = qs.annotate(
            user_has_upvoted_annotated=Exists(
                PostUpvote.objects.filter(post=OuterRef("pk"), user=user)
            ),
            is_saved_annotated=Exists(
                SavedPost.objects.filter(post=OuterRef("pk"), user=user)
            ),
            is_following_annotated=Exists(
                Follow.objects.filter(
                    user=user, target_type=Follow.TARGET_QUESTION,
                    target_key=Cast(OuterRef("pk"), output_field=CharField()),
                )
            ),
        )
    return qs


def _toggle_follow(user, target_type, target_key):
    """Toggle a Follow row. Returns True if now following, False if removed."""
    obj, created = Follow.objects.get_or_create(
        user=user, target_type=target_type, target_key=str(target_key)
    )
    if not created:
        obj.delete()
        return False
    return True


def _save_attachments(request, post):
    """Persist any multipart `files` uploaded with a thread. Silently caps each
    file at FORUM_MAX_ATTACHMENT_MB and ignores oversize files."""
    from django.conf import settings as dj_settings
    max_mb = int(getattr(dj_settings, "FORUM_MAX_ATTACHMENT_MB", 15))
    limit = max_mb * 1024 * 1024
    files = request.FILES.getlist("files") if hasattr(request, "FILES") else []
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    for f in files[:10]:
        if f.size > limit:
            continue
        name = (f.name or "").lower()
        kind = Attachment.KIND_IMAGE if name.endswith(image_exts) else Attachment.KIND_FILE
        Attachment.objects.create(
            post=post, file=f, kind=kind,
            original_name=f.name or "", uploaded_by=request.user,
        )


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

        tag = request.query_params.get("tag") or request.query_params.get("topic")
        if tag:
            qs = qs.filter(tags__name__iexact=tag)

        author = request.query_params.get("author")
        if author:
            qs = qs.filter(author__username=author)

        kind = request.query_params.get("kind")
        if kind in (ForumPost.KIND_QUESTION, ForumPost.KIND_POST):
            qs = qs.filter(kind=kind)

        space = request.query_params.get("space")
        if space:
            qs = qs.filter(space__slug=space)

        solved = request.query_params.get("solved")
        if solved == "true":
            qs = qs.filter(is_solved=True)
        elif solved == "unanswered":
            qs = qs.filter(answer_count_annotated=0)
        elif solved == "false":
            qs = qs.filter(is_solved=False)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        # Feed tabs from the redesign map onto the same queryset.
        sort = request.query_params.get("sort", "newest")
        if sort in ("oldest",):
            qs = qs.order_by("created_at")
        elif sort in ("popular", "trending"):
            qs = qs.order_by("-answer_count_annotated", "-upvote_count", "-created_at")
        elif sort == "unanswered":
            qs = qs.filter(answer_count_annotated=0).order_by("-created_at")
        else:  # newest / latest / foryou
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
        banned = _ban_error(request.user)
        if banned is not None:
            return banned

        serializer = CreateThreadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        title = serializer.validated_data["title"]
        body = serializer.validated_data.get("body", "")
        kind = serializer.validated_data.get("kind", ForumPost.KIND_QUESTION)
        space_slug = (serializer.validated_data.get("space") or "").strip()
        tag_names = serializer.validated_data.get("tags", [])

        # The scanner runs BEFORE the real post is created. A flagged
        # submission never becomes a visible ForumPost — it's queued for a
        # moderator to confirm-delete or override-restore (see
        # AutoRejectedSubmission / forum/moderation_views.py).
        categories = forum_moderation.scan_content(title, body)
        if categories:
            _queue_auto_rejected(
                request.user, kind, title, body, categories, tags=tag_names,
            )
            return Response(
                {"status": "pending_review",
                 "detail": "Your submission has been received and is awaiting a routine review before it appears publicly."},
                status=status.HTTP_200_OK,
            )

        space = None
        if space_slug:
            space = Space.objects.filter(slug=space_slug).first()

        post = ForumPost.objects.create(
            author=request.user,
            title=title,
            content=body,
            kind=kind,
            space=space,
        )

        for name in tag_names:
            clean = name.lower().strip()
            if clean:
                tag, _ = Tag.objects.get_or_create(name=clean)
                post.tags.add(tag)

        # Optional file/image attachments (multipart `files`).
        _save_attachments(request, post)

        # NOTE: the old notify-EVERY-user fan-out (N rows + N Celery tasks per
        # thread) is intentionally gone. Threads are discoverable in the list;
        # people are notified when someone engages with THEM (replies below).

        post = _annotated_threads(request.user).get(pk=post.pk)
        return Response(
            ForumPostSerializer(post, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


def _register_view(request, post):
    """Count one view per browser session per thread, not per request —
    so refreshing or re-opening the same thread doesn't inflate the count.
    Capped so the session payload can't grow unbounded over a long visit."""
    session = request.session
    seen = session.get("forum_viewed_threads", [])
    key = str(post.pk)
    if key in seen:
        return
    ForumPost.objects.filter(pk=post.pk).update(view_count=F("view_count") + 1)
    post.view_count = (post.view_count or 0) + 1
    seen.append(key)
    session["forum_viewed_threads"] = seen[-500:]
    session.modified = True


class ThreadDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, thread_id):
        post = get_object_or_404(_annotated_threads(request.user), pk=thread_id)
        _register_view(request, post)

        replies = (
            Reply.objects.filter(post=post)
            .select_related("author", "author__forum_profile")
            .prefetch_related("author__identities")
            .annotate(upvote_count=Count("upvotes", distinct=True))
        )
        if request.user.is_authenticated:
            replies = replies.annotate(
                user_has_upvoted_annotated=Exists(
                    ReplyUpvote.objects.filter(reply=OuterRef("pk"), user=request.user)
                )
            )

        answers = [r for r in replies if r.kind == Reply.KIND_ANSWER]
        # Accepted answer floats to the top, then most-upvoted, then oldest.
        answers.sort(key=lambda r: (
            0 if post.accepted_reply_id == r.id else 1,
            -(r.upvote_count or 0),
            r.created_at,
        ))
        comments = [r for r in replies if r.kind == Reply.KIND_COMMENT]
        comments.sort(key=lambda r: r.created_at)

        ctx = {"request": request}
        return Response({
            **ForumPostSerializer(post, context=ctx).data,
            "answers": ReplySerializer(answers, many=True, context=ctx).data,
            "comments": ReplySerializer(comments, many=True, context=ctx).data,
        })


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
        banned = _ban_error(request.user)
        if banned is not None:
            return banned

        post = get_object_or_404(ForumPost, pk=thread_id)
        locked = _lock_error(post)
        if locked is not None:
            return locked
        serializer = CreateCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        content = serializer.validated_data["content"]
        kind = serializer.validated_data.get("kind", Reply.KIND_ANSWER)

        categories = forum_moderation.scan_content("", content)
        if categories:
            _queue_auto_rejected(
                request.user, kind, "", content, categories, thread=post,
            )
            return Response(
                {"status": "pending_review",
                 "detail": "Your submission has been received and is awaiting a routine review before it appears publicly."},
                status=status.HTTP_200_OK,
            )

        # Answers are flat; only comments thread under a parent comment.
        reply_to_id = serializer.validated_data.get("reply_to_comment_id")
        reply_to = None
        if reply_to_id and kind == Reply.KIND_COMMENT:
            reply_to = get_object_or_404(Reply, pk=reply_to_id, post=post)

        reply = Reply.objects.create(
            post=post,
            author=request.user,
            content=content,
            reply_to=reply_to,
            kind=kind,
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
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
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
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        reply = get_object_or_404(Reply, pk=comment_id)
        upvote, created = ReplyUpvote.objects.get_or_create(
            user=request.user, reply=reply
        )
        if not created:
            upvote.delete()
            return Response({"upvoted": False, "upvote_count": reply.upvotes.count()})
        return Response({"upvoted": True, "upvote_count": reply.upvotes.count()})


# =====================================================
# Accept Answer
# =====================================================
class AcceptAnswerView(APIView):
    """Toggle a reply as the accepted answer for its thread. Only the
    thread's author (or staff) may accept; calling again on the same reply
    un-accepts it. Marks the thread solved/unsolved to match."""
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id, reply_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        if post.author_id != request.user.id and not request.user.is_staff:
            return Response(
                {"detail": "Only the thread author can accept an answer."},
                status=status.HTTP_403_FORBIDDEN,
            )
        reply = get_object_or_404(Reply, pk=reply_id, post=post)

        if post.accepted_reply_id == reply.id:
            post.accepted_reply = None
            post.is_solved = False
            post.save(update_fields=["accepted_reply", "is_solved"])
            return Response({"accepted_reply_id": None, "is_solved": False})

        post.accepted_reply = reply
        post.is_solved = True
        post.save(update_fields=["accepted_reply", "is_solved"])

        if reply.author_id != request.user.id:
            message = f'{request.user.username} accepted your answer on "{post.title}"'
            notify(
                recipient=reply.author,
                actor=request.user,
                verb="forum.accepted",
                title=message,
                body=message,
                link_url=f"/forum/{post.id}",
                payload={
                    "thread_id": post.id,
                    "title": post.title,
                    "legacy_type": "accepted_answer",
                },
                ws_extra={
                    "type": "forum",
                    "notification_type": "accepted_answer",
                    "message": message,
                    "thread_id": str(post.id),
                },
            )

        return Response({"accepted_reply_id": reply.id, "is_solved": True})


# =====================================================
# Public Forum Profile
# =====================================================
class PublicForumProfileView(APIView):
    """GET /forum/users/:username/ — a lightweight public profile. Stats are
    computed on read from existing forum data; nothing here duplicates
    account-level personal info (name, phone, etc. stay private)."""
    permission_classes = [AllowAny]

    def get(self, request, username):
        user = get_object_or_404(User, username=username)
        profile = getattr(user, "forum_profile", None)

        thread_count = ForumPost.objects.filter(author=user).count()
        reply_count = Reply.objects.filter(author=user).count()
        upvotes_received = (
            PostUpvote.objects.filter(post__author=user).count()
            + ReplyUpvote.objects.filter(reply__author=user).count()
        )

        badge = author_badge(user)
        data = {
            "username": user.username,
            "display_name": badge["display_name"],
            "headline": (profile.headline if profile else "") or badge["credential"],
            "location": profile.location if profile else "",
            "initials": badge["initials"],
            "color": badge["color"],
            "avatar_url": badge["avatar_url"],
            "joined_at": user.date_joined,
            "bio": profile.bio if profile else "",
            "thread_count": thread_count,
            "reply_count": reply_count,
            "upvotes_received": upvotes_received,
            "is_self": bool(request.user.is_authenticated and request.user.id == user.id),
        }
        return Response(PublicForumProfileSerializer(data).data)


class UpdateForumProfileView(APIView):
    """PATCH /forum/profile/ — update the caller's own forum profile."""
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        profile, _ = ForumProfile.objects.get_or_create(user=request.user)
        serializer = UpdateForumProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({
            "display_name": profile.display_name,
            "headline": profile.headline,
            "location": profile.location,
            "bio": profile.bio,
        })


class ListUserRepliesView(APIView):
    """GET /forum/users/:username/replies/ — a user's replies, newest first,
    each carrying its parent thread's id + title so the profile page can
    link back to the discussion. Mirrors ListThreadsView's pagination."""
    permission_classes = [AllowAny]

    def get(self, request, username):
        user = get_object_or_404(User, username=username)
        qs = Reply.objects.filter(author=user).select_related("post").annotate(
            upvote_count=Count("upvotes", distinct=True),
        ).order_by("-created_at")

        if request.user.is_authenticated:
            qs = qs.annotate(
                user_has_upvoted_annotated=Exists(
                    ReplyUpvote.objects.filter(reply=OuterRef("pk"), user=request.user)
                )
            )

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        replies = qs[start:start + page_size]

        serializer = UserReplySerializer(replies, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": total})


# =====================================================
# Topics & Categories (fixed taxonomy; counts computed on read)
# =====================================================
def _category_payload(cat, user):
    q_count = ForumPost.objects.filter(tags__name__iexact=cat["topic"]).distinct().count()
    followers = Follow.objects.filter(
        target_type=Follow.TARGET_CATEGORY, target_key=cat["id"]).count()
    is_following = False
    if user and user.is_authenticated:
        is_following = Follow.objects.filter(
            user=user, target_type=Follow.TARGET_CATEGORY, target_key=cat["id"]
        ).exists()
    return {**cat, "question_count": q_count,
            "follower_count": followers, "is_following": is_following}


class ListTopicsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            "topics": FORUM_TOPICS,
            "categories": [_category_payload(c, request.user) for c in FORUM_CATEGORIES],
        })


class ListCategoriesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        data = [_category_payload(c, request.user) for c in FORUM_CATEGORIES]
        return Response({"results": data, "count": len(data)})


class CategoryDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, category_id):
        cat = FORUM_CATEGORIES_BY_ID.get(category_id)
        if not cat:
            return Response({"detail": "Category not found."}, status=status.HTTP_404_NOT_FOUND)
        qs = _annotated_threads(request.user).filter(
            tags__name__iexact=cat["topic"]).distinct()
        sort = request.query_params.get("sort", "latest")
        if sort in ("trending", "popular"):
            qs = qs.order_by("-answer_count_annotated", "-upvote_count", "-created_at")
        else:
            qs = qs.order_by("-created_at")

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        posts = qs[start:start + page_size]
        return Response({
            "category": _category_payload(cat, request.user),
            "results": ForumPostSerializer(posts, many=True, context={"request": request}).data,
            "count": total,
        })


# =====================================================
# Spaces
# =====================================================
def _spaces_qs():
    return Space.objects.select_related("creator").annotate(
        question_count_annotated=Count("posts", distinct=True))


class ListSpacesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        spaces = _spaces_qs().order_by("-created_at")
        data = SpaceSerializer(spaces, many=True, context={"request": request}).data
        return Response({"results": data, "count": len(data)})


class CreateSpaceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        serializer = CreateSpaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"].strip()
        if not name:
            return Response({"detail": "Please enter a Space name."},
                            status=status.HTTP_400_BAD_REQUEST)
        desc = serializer.validated_data.get("description", "")
        blocked = _moderation_error(f"{name}\n{desc}")
        if blocked is not None:
            return blocked
        color = FORUM_PALETTE[Space.objects.count() % len(FORUM_PALETTE)]
        space = Space.objects.create(
            name=name, description=desc,
            topic=serializer.validated_data.get("topic", ""),
            color=color, creator=request.user,
        )
        # The creator is the first member (member == follower).
        Follow.objects.get_or_create(
            user=request.user, target_type=Follow.TARGET_SPACE, target_key=space.slug)
        space = _spaces_qs().get(pk=space.pk)
        return Response(SpaceSerializer(space, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class SpaceDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        space = get_object_or_404(_spaces_qs(), slug=slug)
        qs = _annotated_threads(request.user).filter(space=space).order_by("-created_at")
        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        posts = qs[start:start + page_size]
        return Response({
            "space": SpaceSerializer(space, context={"request": request}).data,
            "results": ForumPostSerializer(posts, many=True, context={"request": request}).data,
            "count": total,
        })


class FollowSpaceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, slug):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        space = get_object_or_404(Space, slug=slug)
        following = _toggle_follow(request.user, Follow.TARGET_SPACE, space.slug)
        member_count = Follow.objects.filter(
            target_type=Follow.TARGET_SPACE, target_key=space.slug).count()
        return Response({"following": following, "member_count": member_count})


# =====================================================
# Follows (question / category) + Saved bookmarks
# =====================================================
class FollowThreadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        post = get_object_or_404(ForumPost, pk=thread_id)
        following = _toggle_follow(request.user, Follow.TARGET_QUESTION, post.id)
        return Response({"following": following})


class FollowCategoryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, category_id):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        if category_id not in FORUM_CATEGORIES_BY_ID:
            return Response({"detail": "Category not found."},
                            status=status.HTTP_404_NOT_FOUND)
        following = _toggle_follow(request.user, Follow.TARGET_CATEGORY, category_id)
        return Response({"following": following})


class ToggleSaveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        post = get_object_or_404(ForumPost, pk=thread_id)
        obj, created = SavedPost.objects.get_or_create(user=request.user, post=post)
        if not created:
            obj.delete()
            return Response({"saved": False})
        return Response({"saved": True})


class ListSavedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        saved_ids = list(SavedPost.objects.filter(user=request.user)
                         .values_list("post_id", flat=True))
        qs = _annotated_threads(request.user).filter(id__in=saved_ids).order_by("-created_at")
        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        posts = qs[start:start + page_size]
        return Response({
            "results": ForumPostSerializer(posts, many=True, context={"request": request}).data,
            "count": total,
        })


# =====================================================
# Answer queue — questions still needing an answer
# =====================================================
class AnswerQueueView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        answered = list(Reply.objects.filter(
            author=request.user, kind=Reply.KIND_ANSWER
        ).values_list("post_id", flat=True))
        qs = (_annotated_threads(request.user)
              .filter(kind=ForumPost.KIND_QUESTION, answer_count_annotated=0)
              .exclude(author=request.user)
              .exclude(id__in=answered)
              .order_by("-created_at"))
        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 10, 50)
        total = qs.count()
        start = (page - 1) * page_size
        posts = qs[start:start + page_size]
        return Response({
            "results": ForumPostSerializer(posts, many=True, context={"request": request}).data,
            "count": total,
        })


# =====================================================
# Search — questions / people / tags / categories
# =====================================================
class SearchView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        empty = {"query": q, "questions": [], "users": [], "tags": [], "categories": []}
        if not q:
            return Response(empty)

        posts = _annotated_threads(request.user).filter(
            Q(title__icontains=q) | Q(tags__name__icontains=q) | Q(content__icontains=q)
        ).distinct().order_by("-created_at")[:20]

        users_qs = User.objects.filter(
            Q(username__icontains=q)
            | Q(forum_profile__display_name__icontains=q)
            | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).distinct()[:15]

        tags = [{"label": t.name} for t in Tag.objects.filter(name__icontains=q)[:15]]
        ql = q.lower()
        cats = [_category_payload(c, request.user) for c in FORUM_CATEGORIES
                if ql in c["name"].lower() or ql in c["desc"].lower()]

        return Response({
            "query": q,
            "questions": ForumPostSerializer(posts, many=True, context={"request": request}).data,
            "users": [author_badge(u) for u in users_qs],
            "tags": tags,
            "categories": cats,
        })


# =====================================================
# Report
# =====================================================
class CreateReportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreateReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_type = serializer.validated_data["target_type"]
        target_id = serializer.validated_data["target_id"]
        if target_type == "question":
            obj = get_object_or_404(ForumPost, pk=target_id)
            ct = ContentType.objects.get_for_model(ForumPost)
        else:  # answer / comment
            obj = get_object_or_404(Reply, pk=target_id)
            ct = ContentType.objects.get_for_model(Reply)
        Report.objects.create(
            reporter=request.user, content_type=ct, object_id=obj.id,
            reason=serializer.validated_data["reason"],
            detail=serializer.validated_data.get("detail", ""),
        )
        return Response({"detail": "Reported to moderators — thanks for flagging."},
                        status=status.HTTP_201_CREATED)


# =====================================================
# Current user's forum context (hydration)
# =====================================================
class ForumMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        profile, _ = ForumProfile.objects.get_or_create(user=u)
        badge = author_badge(u)
        saved = list(SavedPost.objects.filter(user=u).values_list("post_id", flat=True))
        following = {"spaces": [], "questions": [], "categories": []}
        for f in Follow.objects.filter(user=u):
            if f.target_type == Follow.TARGET_SPACE:
                following["spaces"].append(f.target_key)
            elif f.target_type == Follow.TARGET_QUESTION:
                following["questions"].append(
                    int(f.target_key) if f.target_key.isdigit() else f.target_key)
            elif f.target_type == Follow.TARGET_CATEGORY:
                following["categories"].append(f.target_key)
        return Response({
            **badge,
            "headline": profile.headline or badge["credential"],
            "location": profile.location,
            "bio": profile.bio,
            "saved": saved,
            "following": following,
        })
