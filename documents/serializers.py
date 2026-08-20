# Explore document-library serializers.
#
# Read serializers are ModelSerializers whose method fields are annotation-aware
# (they check getattr(obj, "<x>_annotated", None) first, set by the view's base
# queryset, and only fall back to a live query otherwise — avoids N+1). Write
# input uses plain Serializers. Output shapes intentionally track the frontend's
# existing exploreApi mock (type / typeMeta / dateLabel / views / downloads) so
# flipping USE_MOCK=false needs minimal card changes.

from rest_framework import serializers

from .models import (
    Document, DocumentCategory, Collection, Follow, Report,
    SavedDocument, DocumentLike,
)
from .utils import contributor_badge


def _date_label(dt):
    return dt.strftime("%b %Y") if dt else ""


# =====================================================
# Category
# =====================================================
class DocumentCategorySerializer(serializers.ModelSerializer):
    key = serializers.CharField(source="slug")
    count = serializers.SerializerMethodField()

    class Meta:
        model = DocumentCategory
        fields = ("key", "name", "icon", "color", "blurb", "order", "count")

    def get_count(self, obj):
        annotated = getattr(obj, "doc_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.documents.filter(is_removed=False).count()


class CategoryWriteSerializer(serializers.Serializer):
    """Moderator create/update input for DocumentCategory."""
    name = serializers.CharField(max_length=120)
    slug = serializers.SlugField(max_length=140, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    icon = serializers.CharField(required=False, allow_blank=True, default="", max_length=8)
    color = serializers.CharField(required=False, allow_blank=True, default="#125027", max_length=9)
    blurb = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    order = serializers.IntegerField(required=False, default=0, min_value=0)
    is_active = serializers.BooleanField(required=False, default=True)


# =====================================================
# Document — card + detail
# =====================================================
class DocumentCardSerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()
    typeMeta = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    desc = serializers.CharField(source="description", read_only=True)
    date = serializers.SerializerMethodField()
    dateLabel = serializers.SerializerMethodField()
    views = serializers.IntegerField(source="view_count", read_only=True)
    downloads = serializers.IntegerField(source="download_count", read_only=True)
    file_url = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id", "title", "type", "typeMeta", "author", "subject", "level",
            "language", "institution", "filetype", "date", "dateLabel", "pages",
            "views", "downloads", "rating", "tags", "desc", "file_url",
            "is_saved", "is_liked", "likes_count", "created_at",
        )

    def get_type(self, obj):
        return obj.category.slug if obj.category_id else "other"

    def get_typeMeta(self, obj):
        c = obj.category
        if not c:
            return {"key": "other", "name": "Other", "icon": "📄", "color": "#125027"}
        return {"key": c.slug, "name": c.name, "icon": c.icon, "color": c.color}

    def get_author(self, obj):
        return contributor_badge(obj.owner)

    def get_tags(self, obj):
        return list(obj.tags.values_list("name", flat=True))

    def get_date(self, obj):
        return obj.created_at.date().isoformat() if obj.created_at else ""

    def get_dateLabel(self, obj):
        return _date_label(obj.created_at)

    def get_file_url(self, obj):
        if not obj.file:
            return ""
        request = self.context.get("request")
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url

    def get_is_saved(self, obj):
        annotated = getattr(obj, "is_saved_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.saved_by.filter(user=request.user).exists()
        return False

    def get_is_liked(self, obj):
        annotated = getattr(obj, "is_liked_annotated", None)
        if annotated is not None:
            return annotated
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.likes.filter(user=request.user).exists()
        return False

    def get_likes_count(self, obj):
        annotated = getattr(obj, "likes_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.likes.count()


class DocumentDetailSerializer(DocumentCardSerializer):
    full = serializers.CharField(read_only=True)

    class Meta(DocumentCardSerializer.Meta):
        fields = DocumentCardSerializer.Meta.fields + ("full",)


class UploadDocumentSerializer(serializers.Serializer):
    """Multipart create input for POST /explore/documents/."""
    title = serializers.CharField(max_length=300)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    full = serializers.CharField(required=False, allow_blank=True, default="")
    category = serializers.CharField(required=False, allow_blank=True, default="")  # slug
    subject = serializers.CharField(required=False, allow_blank=True, default="", max_length=120)
    level = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)
    language = serializers.CharField(required=False, allow_blank=True, default="English", max_length=40)
    institution = serializers.CharField(required=False, allow_blank=True, default="", max_length=160)
    filetype = serializers.CharField(required=False, allow_blank=True, default="PDF", max_length=10)
    pages = serializers.IntegerField(required=False, default=0, min_value=0)
    tags = serializers.CharField(required=False, allow_blank=True, default="")  # comma-joined
    file = serializers.FileField(required=False)

    def validate_file(self, value):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from config.upload_validation import validate_upload, DOCUMENT_EXTS
        try:
            validate_upload(value, DOCUMENT_EXTS, max_mb=50)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message)
        return value


# =====================================================
# Collection
# =====================================================
class CollectionSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="slug")
    desc = serializers.CharField(source="description", read_only=True)
    curator = serializers.SerializerMethodField()
    count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = ("id", "title", "desc", "color", "visibility", "count", "curator", "created_at")

    def get_curator(self, obj):
        return contributor_badge(obj.curator) if obj.curator_id else None

    def get_count(self, obj):
        annotated = getattr(obj, "doc_count_annotated", None)
        if annotated is not None:
            return annotated
        return obj.documents.filter(is_removed=False).count()


class CollectionWriteSerializer(serializers.Serializer):
    """Create/update input for a user's own collection. Used with
    partial=True for PATCH — fields absent from the payload are left
    untouched by the view rather than reset to their default."""
    title = serializers.CharField(max_length=180)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    color = serializers.CharField(required=False, allow_blank=True, default="#125027", max_length=9)
    visibility = serializers.ChoiceField(
        choices=[c[0] for c in Collection.VIS_CHOICES],
        required=False, default=Collection.VIS_PUBLIC,
    )


class AddCollectionDocumentSerializer(serializers.Serializer):
    """Input for POST /explore/collections/<slug>/documents/."""
    document_id = serializers.IntegerField()


# =====================================================
# Report
# =====================================================
class CreateReportSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=[c[0] for c in Report.REASON_CHOICES])
    detail = serializers.CharField(required=False, allow_blank=True, default="")
