from rest_framework import serializers
from .models import Tag, ForumPost, Reply, PostUpvote, ReplyUpvote


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
            "user_has_upvoted",
        )

    def get_author_username(self, obj):
        return obj.author.username

    def get_tags(self, obj):
        return list(obj.tags.values_list("name", flat=True))

    user_has_upvoted = serializers.SerializerMethodField()

    def get_user_has_upvoted(self, obj):
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
        )

    def get_author_username(self, obj):
        return obj.author.username

    user_has_upvoted = serializers.SerializerMethodField()

    def get_user_has_upvoted(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.upvotes.filter(user=request.user).exists()
        return False


class CreateCommentSerializer(serializers.Serializer):
    content = serializers.CharField()
    reply_to_comment_id = serializers.IntegerField(required=False, default=None)
