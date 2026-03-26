from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q

from .models import Tag, ForumPost, Reply, PostUpvote, ReplyUpvote
from .serializers import (
    TagSerializer,
    ForumPostSerializer,
    CreateThreadSerializer,
    CommentSerializer,
    CreateCommentSerializer,
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
        qs = ForumPost.objects.select_related("author").prefetch_related("tags").annotate(
            reply_count=Count("replies"),
            upvote_count=Count("upvotes"),
        )

        # Search by title or content
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(content__icontains=search))

        # Filter by tag name
        tag = request.query_params.get("tag")
        if tag:
            qs = qs.filter(tags__name=tag)

        # Filter by date range
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        # Sort
        sort = request.query_params.get("sort", "newest")
        if sort == "oldest":
            qs = qs.order_by("created_at")
        else:
            qs = qs.order_by("-created_at")

        # Pagination
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 10))
        total = qs.count()
        start = (page - 1) * page_size
        end = start + page_size
        threads = qs[start:end]

        serializer = ForumPostSerializer(threads, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": total})


class CreateThreadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreateThreadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        post = ForumPost.objects.create(
            author=request.user,
            title=serializer.validated_data["title"],
            content=serializer.validated_data.get("body", ""),
        )

        # Handle tags
        tag_names = serializer.validated_data.get("tags", [])
        for name in tag_names:
            tag, _ = Tag.objects.get_or_create(name=name.lower().strip())
            post.tags.add(tag)

        # Re-fetch with annotations for response
        post = ForumPost.objects.filter(pk=post.pk).annotate(
            reply_count=Count("replies"),
            upvote_count=Count("upvotes"),
        ).select_related("author").prefetch_related("tags").first()

        return Response(
            ForumPostSerializer(post, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ThreadDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, thread_id):
        post = get_object_or_404(
            ForumPost.objects.annotate(
                reply_count=Count("replies"),
                upvote_count=Count("upvotes"),
            ).select_related("author").prefetch_related("tags"),
            pk=thread_id,
        )
        return Response(ForumPostSerializer(post, context={"request": request}).data)


class DeleteThreadView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, thread_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        if post.author != request.user:
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
            upvote_count=Count("upvotes"),
        )

        # Sort
        sort = request.query_params.get("sort", "oldest")
        if sort == "newest":
            qs = qs.order_by("-created_at")
        else:
            qs = qs.order_by("created_at")

        total = qs.count()
        serializer = CommentSerializer(qs, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": total})


class CreateCommentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        post = get_object_or_404(ForumPost, pk=thread_id)
        serializer = CreateCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reply_to_id = serializer.validated_data.get("reply_to_comment_id")
        reply_to = None
        if reply_to_id:
            reply_to = get_object_or_404(Reply, pk=reply_to_id, post=post)

        reply = Reply.objects.create(
            post=post,
            author=request.user,
            content=serializer.validated_data["content"],
            reply_to=reply_to,
        )

        # Re-fetch with annotation
        reply = Reply.objects.filter(pk=reply.pk).annotate(
            upvote_count=Count("upvotes"),
        ).select_related("author").first()

        return Response(
            CommentSerializer(reply, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class DeleteCommentView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, comment_id):
        reply = get_object_or_404(Reply, pk=comment_id)
        if reply.author != request.user:
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
