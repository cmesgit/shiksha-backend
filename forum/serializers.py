# PLACEMENT: backend/backend/forum/serializers.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/forum/serializers.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# NotificationSerializer is GONE — the legacy response shape now lives in
# notifications/serializers.py (LegacyForumNotificationSerializer) and is
# served on the same old routes. Tag/thread/comment serializers unchanged.

from rest_framework import serializers
from .models import Tag, ForumPost, Reply, PostUpvote, ReplyUpvote, ForumProfile


# =====================================================
# Tag Serializer
# =====================================================
class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("id", "name")


# =====================================================
# Forum Post (Thread) Serializers
# =====================================================
class ForumPostSerializer(serializers.ModelSerializer):
    author_username = serializers.SerializerMethodField()
    body = serializers.CharField(source="content", read_only=True)
    tags = serializers.SerializerMethodField()
    reply_count = serializers.IntegerField(read_only=True)
    upvote_count = serializers.IntegerField(read_only=True)
    view_count = serializers.IntegerField(read_only=True)
    is_solved = serializers.BooleanField(read_only=True)
    accepted_reply_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = ForumPost
        fields = (
            "id",
            "title",
            "body",
            "author_username",
            "created_at",
            "tags",
            "reply_count",
            "upvote_count",
            "view_count",
            "is_solved",
            "accepted_reply_id",
            "user_has_upvoted",
        )

    def get_author_username(self, obj):
        return obj.author.username

    def get_tags(self, obj):
        return list(obj.tags.values_list("name", flat=True))

    user_has_upvoted = serializers.SerializerMethodField()

    def get_user_has_upvoted(self, obj):
        annotated = getattr(obj, "user_has_upvoted_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.upvotes.filter(user=request.user).exists()
        return False


class CreateThreadSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=300)
    body = serializers.CharField(required=False, default="", allow_blank=True)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        default=list,
    )


# =====================================================
# Comment (Reply) Serializers
# =====================================================
class CommentSerializer(serializers.ModelSerializer):
    thread_id = serializers.IntegerField(source="post_id", read_only=True)
    author_username = serializers.SerializerMethodField()
    reply_to_comment_id = serializers.IntegerField(
        source="reply_to_id", read_only=True
    )
    upvote_count = serializers.IntegerField(read_only=True)
    is_accepted = serializers.SerializerMethodField()

    class Meta:
        model = Reply
        fields = (
            "id",
            "thread_id",
            "author_username",
            "content",
            "created_at",
            "reply_to_comment_id",
            "upvote_count",
            "user_has_upvoted",
            "is_accepted",
        )

    def get_author_username(self, obj):
        return obj.author.username

    def get_is_accepted(self, obj):
        return obj.post.accepted_reply_id == obj.id

    user_has_upvoted = serializers.SerializerMethodField()

    def get_user_has_upvoted(self, obj):
        annotated = getattr(obj, "user_has_upvoted_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.upvotes.filter(user=request.user).exists()
        return False


class CreateCommentSerializer(serializers.Serializer):
    content = serializers.CharField()
    reply_to_comment_id = serializers.IntegerField(required=False, default=None)


class UserReplySerializer(serializers.ModelSerializer):
    """Shape for GET /forum/users/:username/replies/ — a reply plus just
    enough thread context (id + title) to link back to the discussion it
    belongs to, for the profile page's Replies tab."""
    thread_id = serializers.IntegerField(source="post_id", read_only=True)
    thread_title = serializers.CharField(source="post.title", read_only=True)
    upvote_count = serializers.IntegerField(read_only=True)
    is_accepted = serializers.SerializerMethodField()
    user_has_upvoted = serializers.SerializerMethodField()

    class Meta:
        model = Reply
        fields = (
            "id",
            "thread_id",
            "thread_title",
            "content",
            "created_at",
            "upvote_count",
            "user_has_upvoted",
            "is_accepted",
        )

    def get_is_accepted(self, obj):
        return obj.post.accepted_reply_id == obj.id

    def get_user_has_upvoted(self, obj):
        annotated = getattr(obj, "user_has_upvoted_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.upvotes.filter(user=request.user).exists()
        return False


# =====================================================
# Public Forum Profile
# =====================================================
class PublicForumProfileSerializer(serializers.Serializer):
    """Read-only shape for GET /forum/users/:username/. Stats are computed
    on read in the view (not stored), so this serializer just declares
    the response shape for documentation / consistency."""
    username = serializers.CharField()
    joined_at = serializers.DateTimeField()
    bio = serializers.CharField()
    thread_count = serializers.IntegerField()
    reply_count = serializers.IntegerField()
    upvotes_received = serializers.IntegerField()
    is_self = serializers.BooleanField()


class UpdateForumProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForumProfile
        fields = ("bio",)
