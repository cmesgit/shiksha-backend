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
from .taxonomy import normalise_subject_topic


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
    research = serializers.SerializerMethodField()
    verification = serializers.SerializerMethodField()
    topics = serializers.SerializerMethodField()
    files = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id", "title", "type", "typeMeta", "author", "subject", "topic", "level",
            "language", "institution", "filetype", "date", "dateLabel", "pages",
            "views", "downloads", "rating", "tags", "desc", "file_url",
            "is_saved", "is_liked", "likes_count", "created_at", "research",
            "verification", "topics", "files",
        )

    def get_verification(self, obj):
        """The Verified/Unverified badge and its progress line.

        ⚠ `None` while the feature flag is off, and every consumer must treat
        that as "render no badge" rather than "unverified". Those are different
        statements: one says nothing has been checked, the other says checking
        does not exist here yet, and showing an amber "Unverified" pill on
        every document in the library the day before launch would be the
        second dressed up as the first.

        ⚠ This is also the hand-rolled-dict trap this codebase has hit more
        than six times: the model has carried `verification_status` since
        Phase 2, but until it was named HERE no screen could see it.
        """
        from global_settings.models import GlobalSettings
        if not GlobalSettings.load().explore_verification_enabled:
            return None

        from . import verification as v
        from .verification_serializers import VerificationProgressSerializer

        data = VerificationProgressSerializer(v.progress(obj)).data

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            allowed, _reason = v.can_review(user, obj)
            data["can_review"] = allowed
            data["my_subject"] = v.matching_subject(user, obj) if allowed else ""
        else:
            data["can_review"] = False
            data["my_subject"] = ""
        return data

    def get_topics(self, obj):
        """The `field › subject › branch` paths. Several per set."""
        return [
            {"field": t.field, "subject": t.subject, "branch": t.branch,
             "path": str(t)}
            for t in obj.topics.all()
        ]

    def get_files(self, obj):
        out = []
        for f in obj.files.all():
            try:
                url = f.file.url
            except Exception:
                url = ""
            out.append({
                "id": f.id,
                "name": f.original_name or str(f.file).rsplit("/", 1)[-1],
                "size": f.size, "filetype": f.filetype, "url": url,
            })
        return out

    def get_research(self, obj):
        paper = getattr(obj, "research_submission", None)
        if paper is None or paper.published_version is None or obj.is_removed:
            return None
        return {"version": paper.published_version, "authors": paper.authors,
                "corrections": [{"version": c.version, "public_note": c.public_note, "decided_at": c.decided_at} for c in paper.cases.all() if c.kind == "correction" and c.status == "published"],
                "original_url": paper.original_url, "review_scope": f"{paper.published_review_count} independent subject reviewer(s) and final administrator approval. Findings are not guaranteed."}

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
    verification_detail = serializers.SerializerMethodField()

    class Meta(DocumentCardSerializer.Meta):
        fields = DocumentCardSerializer.Meta.fields + (
            "full", "verification_detail",
        )

    def get_verification_detail(self, obj):
        """The document page's VERIFICATION and HISTORY panels.

        Detail-only, deliberately: it costs two extra queries per document and
        a 24-card browse page has no use for either panel. Putting it on the
        card serializer would make every list page pay for it.

        `None` while the flag is off, for the same reason `verification` is —
        absent and "unverified" are different claims.
        """
        from global_settings.models import GlobalSettings
        if not GlobalSettings.load().explore_verification_enabled:
            return None

        from . import verification as v
        from .models import VerificationReview, VerificationStatus

        reviews = list(
            VerificationReview.objects.filter(document=obj)
            .select_related("reviewer").order_by("created_at")
        )
        current = [r for r in reviews
                   if r.version == obj.verification_version]
        confirmers = [r for r in current if r.outcome == "confirmed"]

        def _name(user):
            return (user.get_full_name() or user.username) if user else ""

        progress = v.progress(obj)
        if obj.verification_status == VerificationStatus.VERIFIED:
            heading = "Verified"
            sentence = (
                f"Confirmed by {progress['confirmations']} specialists in "
                f"{_or_join(v.subjects_of(obj))}."
            )
        elif progress["confirmations"]:
            heading = "Partly confirmed"
            sentence = (
                f"{progress['remaining']} more confirmation"
                f"{'' if progress['remaining'] == 1 else 's'} needed from "
                f"specialists in {_or_join(v.subjects_of(obj))}."
            )
        else:
            heading = "Not checked yet"
            sentence = (
                f"{progress['needed']} specialists in "
                f"{_or_join(v.subjects_of(obj))} need to confirm this set."
            )

        # The history panel. Uploads are derived from the version number
        # rather than stored as events — there is no event table, and
        # inventing one would mean a migration could forge a human action.
        history = [{
            "label": f"Version 1 uploaded by {_name(obj.owner)}",
            "at": obj.created_at,
        }]
        seen_versions = {1}
        for r in reviews:
            if r.version not in seen_versions:
                seen_versions.add(r.version)
            verb = ("Confirmed by" if r.outcome == "confirmed"
                    else "Changes requested by")
            history.append({
                "label": f"{verb} {_name(r.reviewer)}",
                "at": r.created_at,
            })
        for version in range(2, obj.verification_version + 1):
            history.append({
                "label": f"Version {version} uploaded by {_name(obj.owner)}",
                "at": obj.updated_at,
            })
        history.sort(key=lambda h: h["at"])

        return {
            "heading": heading,
            "sentence": sentence,
            "confirmers": [_name(r.reviewer) for r in confirmers],
            "history": history,
        }


def _or_join(values):
    """"Civil Engineering or Sociology" — the design's own phrasing for a set
    tagged under more than one subject, because EITHER qualifies a reviewer."""
    values = [v for v in values if v]
    if not values:
        return "its subjects"
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" or {values[-1]}"


class UploadDocumentSerializer(serializers.Serializer):
    """Multipart create input for POST /explore/documents/."""
    title = serializers.CharField(max_length=300)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    full = serializers.CharField(required=False, allow_blank=True, default="")
    category = serializers.CharField(required=False, allow_blank=True, default="")  # slug
    subject = serializers.CharField(required=False, allow_blank=True, default="", max_length=120)
    topic = serializers.CharField(required=False, allow_blank=True, default="", max_length=120)
    level = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)
    language = serializers.CharField(required=False, allow_blank=True, default="English", max_length=40)
    institution = serializers.CharField(required=False, allow_blank=True, default="", max_length=160)
    filetype = serializers.CharField(required=False, allow_blank=True, default="PDF", max_length=10)
    pages = serializers.IntegerField(required=False, default=0, min_value=0)
    tags = serializers.CharField(required=False, allow_blank=True, default="")  # comma-joined
    file = serializers.FileField(required=False)
    # Several files under one title — the redesign's "set". Repeated `files`
    # parts in the multipart body; `file` stays for the single-file callers
    # that predate this and for the set's first file.
    files = serializers.ListField(
        child=serializers.FileField(), required=False, allow_empty=True,
        default=list,
    )
    # Repeated `topics` parts, each "Field|Subject|Branch". A flat string list
    # rather than nested JSON because this is a multipart form, where nested
    # objects have no natural encoding and every client would invent its own.
    topics = serializers.ListField(
        child=serializers.CharField(max_length=320), required=False,
        allow_empty=True, default=list,
    )

    def validate_files(self, value):
        from config.upload_validation import (
            LIBRARY_EXTS, LIBRARY_MAX_MB, validate_upload,
        )
        from django.core.exceptions import ValidationError as DjangoValidationError

        if len(value) > 20:
            raise serializers.ValidationError(
                "A set can hold up to 20 files. Split larger uploads.")
        for f in value:
            try:
                validate_upload(f, LIBRARY_EXTS, max_mb=LIBRARY_MAX_MB)
            except DjangoValidationError as exc:
                # Name the file. "This file's content doesn't match its
                # extension" is useless when eleven were submitted at once.
                raise serializers.ValidationError(
                    f"{getattr(f, 'name', 'A file')}: {'; '.join(exc.messages)}")
        return value

    def validate(self, attrs):
        subject, topic = normalise_subject_topic(attrs.get("subject", ""), attrs.get("topic", ""))
        if subject.casefold() == "all" or topic.casefold() == "all":
            raise serializers.ValidationError("Use a subject or subcategory name instead of All.")
        if topic and not subject:
            raise serializers.ValidationError({"subject": "Choose or enter a main subject before adding a subcategory."})
        attrs.update(subject=subject, topic=topic)
        return attrs

    def validate_file(self, value):
        """The set's first file. Judged by the SAME rules as `files`.

        ⚠ It used to be DOCUMENT_EXTS at 50 MB. Keeping that here while
        `files` accepted LIBRARY_EXTS at 200 MB would mean the first file a
        member picked was refused and the second accepted — which reads as a
        random failure, not as a rule.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError
        from config.upload_validation import (
            LIBRARY_EXTS, LIBRARY_MAX_MB, validate_upload,
        )
        try:
            validate_upload(value, LIBRARY_EXTS, max_mb=LIBRARY_MAX_MB)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages)
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
