# Forum serializers (redesign).
#
# Field renames preserved from the original API: model `content` is exposed as
# `body` on posts; `post_id`/`reply_to_id` as `thread_id`/`reply_to_comment_id`.
# New in the redesign: kind (question/post, answer/comment), space, saved/follow
# flags, per-row author badge, attachments, and Space/Report inputs.

from rest_framework import serializers

from .models import (
    Tag, ForumPost, Reply, Space, ForumCategory, SavedPost, Follow, Report,
    Attachment, ForumProfile,
)
from .utils import author_badge


# =====================================================
# Tag
# =====================================================
class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("id", "name")


# =====================================================
# Space
# =====================================================
class SpaceMiniSerializer(serializers.Serializer):
    """The compact space blob embedded on a card."""
    slug = serializers.CharField()
    name = serializers.CharField()
    initials = serializers.CharField()
    color = serializers.CharField()


class SpaceSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = Space
        fields = (
            "id", "slug", "name", "description", "initials", "color", "topic",
            "member_count", "question_count", "is_following", "is_mine",
            "created_at",
        )

    def get_member_count(self, obj):
        annotated = getattr(obj, "member_count_annotated", None)
        if annotated is not None:
            return annotated
        return Follow.objects.filter(
            target_type=Follow.TARGET_SPACE, target_key=obj.slug
        ).count()

    def get_question_count(self, obj):
        annotated = getattr(obj, "question_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.posts.count()

    def get_is_following(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return Follow.objects.filter(
                user=request.user, target_type=Follow.TARGET_SPACE,
                target_key=obj.slug,
            ).exists()
        return False

    def get_is_mine(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated
                    and obj.creator_id == request.user.id)


class CreateSpaceSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    topic = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)


# =====================================================
# ForumCategory
# =====================================================
class ForumCategorySerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="slug")
    desc = serializers.CharField(source="description")
    question_count = serializers.SerializerMethodField()
    follower_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = ForumCategory
        fields = (
            "id", "name", "desc", "initials", "color", "topic", "order",
            "question_count", "follower_count", "is_following",
        )

    def get_question_count(self, obj):
        return ForumPost.objects.filter(tags__name__iexact=obj.topic).distinct().count()

    def get_follower_count(self, obj):
        return Follow.objects.filter(
            target_type=Follow.TARGET_CATEGORY, target_key=obj.slug
        ).count()

    def get_is_following(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return Follow.objects.filter(
                user=request.user, target_type=Follow.TARGET_CATEGORY,
                target_key=obj.slug,
            ).exists()
        return False


class CategoryWriteSerializer(serializers.Serializer):
    """Moderator create/update input for ForumCategory."""
    name = serializers.CharField(max_length=120)
    slug = serializers.SlugField(max_length=140, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    initials = serializers.CharField(required=False, allow_blank=True, default="", max_length=4)
    color = serializers.CharField(required=False, allow_blank=True, default="#125027", max_length=9)
    topic = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)
    order = serializers.IntegerField(required=False, default=0, min_value=0)
    is_active = serializers.BooleanField(required=False, default=True)


# =====================================================
# Attachment
# =====================================================
class AttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = ("id", "kind", "original_name", "url")

    def get_url(self, obj):
        if not obj.file:
            return ""
        request = self.context.get("request")
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


# =====================================================
# Forum Post (Thread) — cards + detail
# =====================================================
class ForumPostSerializer(serializers.ModelSerializer):
    author_username = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    body = serializers.CharField(source="content", read_only=True)
    tags = serializers.SerializerMethodField()
    space = serializers.SerializerMethodField()
    reply_count = serializers.IntegerField(read_only=True)
    answer_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    upvote_count = serializers.IntegerField(read_only=True)
    view_count = serializers.IntegerField(read_only=True)
    is_solved = serializers.BooleanField(read_only=True)
    accepted_reply_id = serializers.IntegerField(read_only=True, allow_null=True)
    user_has_upvoted = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = ForumPost
        fields = (
            "id", "kind", "title", "body", "author_username", "author",
            "space", "created_at", "tags",
            "reply_count", "answer_count", "comment_count", "upvote_count",
            "view_count", "is_solved", "accepted_reply_id", "user_has_upvoted",
            "is_saved", "is_following", "attachments",
        )

    def get_author_username(self, obj):
        return obj.author.username

    def get_author(self, obj):
        return author_badge(obj.author)

    def get_tags(self, obj):
        return list(obj.tags.values_list("name", flat=True))

    def get_space(self, obj):
        if not obj.space_id:
            return None
        sp = obj.space
        return {"slug": sp.slug, "name": sp.name,
                "initials": sp.initials, "color": sp.color}

    def get_answer_count(self, obj):
        annotated = getattr(obj, "answer_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.replies.filter(kind=Reply.KIND_ANSWER).count()

    def get_comment_count(self, obj):
        annotated = getattr(obj, "comment_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.replies.filter(kind=Reply.KIND_COMMENT).count()

    def get_user_has_upvoted(self, obj):
        annotated = getattr(obj, "user_has_upvoted_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.upvotes.filter(user=request.user).exists()
        return False

    def get_is_saved(self, obj):
        annotated = getattr(obj, "is_saved_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.saved_by.filter(user=request.user).exists()
        return False

    def get_is_following(self, obj):
        annotated = getattr(obj, "is_following_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return Follow.objects.filter(
                user=request.user, target_type=Follow.TARGET_QUESTION,
                target_key=str(obj.id),
            ).exists()
        return False


class CreateThreadSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=300)
    body = serializers.CharField(required=False, default="", allow_blank=True)
    kind = serializers.ChoiceField(
        choices=[ForumPost.KIND_QUESTION, ForumPost.KIND_POST],
        required=False, default=ForumPost.KIND_QUESTION,
    )
    space = serializers.CharField(required=False, allow_blank=True, default="")  # slug
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False, default=list,
    )


# =====================================================
# Reply — answers + comments (frontend nests comments by reply_to_comment_id)
# =====================================================
class ReplySerializer(serializers.ModelSerializer):
    thread_id = serializers.IntegerField(source="post_id", read_only=True)
    author_username = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    reply_to_comment_id = serializers.IntegerField(source="reply_to_id", read_only=True)
    upvote_count = serializers.IntegerField(read_only=True)
    is_accepted = serializers.SerializerMethodField()
    user_has_upvoted = serializers.SerializerMethodField()

    class Meta:
        model = Reply
        fields = (
            "id", "kind", "thread_id", "author_username", "author", "content",
            "created_at", "reply_to_comment_id", "upvote_count",
            "user_has_upvoted", "is_accepted",
        )

    def get_author_username(self, obj):
        return obj.author.username

    def get_author(self, obj):
        return author_badge(obj.author)

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


# Kept as an alias name for the existing comment endpoints.
CommentSerializer = ReplySerializer


class CreateCommentSerializer(serializers.Serializer):
    content = serializers.CharField()
    kind = serializers.ChoiceField(
        choices=[Reply.KIND_ANSWER, Reply.KIND_COMMENT],
        required=False, default=Reply.KIND_ANSWER,
    )
    reply_to_comment_id = serializers.IntegerField(required=False, default=None, allow_null=True)


class UserReplySerializer(serializers.ModelSerializer):
    """Shape for GET /forum/users/:username/replies/."""
    thread_id = serializers.IntegerField(source="post_id", read_only=True)
    thread_title = serializers.CharField(source="post.title", read_only=True)
    upvote_count = serializers.IntegerField(read_only=True)
    is_accepted = serializers.SerializerMethodField()
    user_has_upvoted = serializers.SerializerMethodField()

    class Meta:
        model = Reply
        fields = (
            "id", "kind", "thread_id", "thread_title", "content", "created_at",
            "upvote_count", "user_has_upvoted", "is_accepted",
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
# Report
# =====================================================
class CreateReportSerializer(serializers.Serializer):
    target_type = serializers.ChoiceField(choices=["question", "answer", "comment"])
    target_id = serializers.IntegerField()
    reason = serializers.ChoiceField(choices=[c[0] for c in Report.REASON_CHOICES])
    detail = serializers.CharField(required=False, allow_blank=True, default="")


# =====================================================
# Forum Profile
# =====================================================
class PublicForumProfileSerializer(serializers.Serializer):
    """Read-only shape for GET /forum/users/:username/."""
    username = serializers.CharField()
    display_name = serializers.CharField()
    headline = serializers.CharField()
    location = serializers.CharField()
    initials = serializers.CharField()
    color = serializers.CharField()
    avatar_url = serializers.CharField()
    joined_at = serializers.DateTimeField()
    bio = serializers.CharField()
    thread_count = serializers.IntegerField()
    reply_count = serializers.IntegerField()
    upvotes_received = serializers.IntegerField()
    is_self = serializers.BooleanField()


class UpdateForumProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForumProfile
        fields = ("display_name", "headline", "location", "bio")
