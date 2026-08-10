# PLACEMENT: backend/backend/counseling/guide_serializers.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/guide_serializers.py

from rest_framework import serializers

from .guide_models import CareerGuide, GuideChapter, GuideSection

# Block types the frontend renderer understands today, plus the ones added
# for the school-level guides (worksheets/action-plans/parent sections).
# Kept here rather than in guide_models.py so the validation list can move
# independently of the schema. An unknown `t` is rejected on WRITE (staff
# CRUD) but never on read — an older frontend build simply renders unknown
# types as null, so backend and frontend can ship out of lockstep.
KNOWN_BLOCK_TYPES = {
    "p", "list", "table", "tip", "ref",
    "h3", "faq", "worksheet", "checklist", "steps", "kv", "note",
}


def validate_blocks(blocks):
    if not isinstance(blocks, list):
        raise serializers.ValidationError("blocks must be a list.")
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or "t" not in block:
            raise serializers.ValidationError(f"blocks[{i}] must be an object with a 't' key.")
        if block["t"] not in KNOWN_BLOCK_TYPES:
            raise serializers.ValidationError(
                f"blocks[{i}]: unknown block type {block['t']!r}. "
                f"Known types: {sorted(KNOWN_BLOCK_TYPES)}"
            )
    return blocks


class GuideSectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuideSection
        fields = (
            "id", "guide", "chapter", "order", "level", "title", "anchor",
            "kind", "audience", "blocks",
        )
        read_only_fields = ("guide",)

    def validate_blocks(self, value):
        return validate_blocks(value)


class GuideChapterSerializer(serializers.ModelSerializer):
    sections = GuideSectionSerializer(many=True, read_only=True)

    class Meta:
        model = GuideChapter
        fields = ("id", "slug", "number", "title", "summary", "order", "kind", "sections")


class GuideChapterIndexSerializer(serializers.ModelSerializer):
    """Chapter list without sections — for the >40-section detail response."""

    section_count = serializers.SerializerMethodField()

    class Meta:
        model = GuideChapter
        fields = ("id", "slug", "number", "title", "summary", "kind", "section_count")

    def get_section_count(self, obj):
        return obj.sections.count()


class CareerGuideCardSerializer(serializers.ModelSerializer):
    """List-view shape. Carries section_count + stage/stage_label/stage_order
    so the frontend never has to derive filter chips from string-splitting
    `audience` — that fragile hack is exactly what LibraryPage.jsx did
    before this endpoint existed."""

    section_count = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()
    specializations = serializers.SlugRelatedField(
        slug_field="name", many=True, read_only=True
    )

    class Meta:
        model = CareerGuide
        fields = (
            "slug", "title", "blurb", "audience", "stage", "stage_label",
            "stage_order", "accent", "cover_url", "class_levels",
            "specializations", "section_count", "view_count",
        )

    def get_section_count(self, obj):
        return getattr(obj, "_section_count", None) or obj.sections.count()

    def get_cover_url(self, obj):
        request = self.context.get("request")
        if not obj.cover:
            return None
        return request.build_absolute_uri(obj.cover.url) if request else obj.cover.url


class CareerGuideDetailSerializer(serializers.ModelSerializer):
    """Full guide payload. `sections` is populated by the view only when
    the guide is small enough to inline (<=40 sections); otherwise it stays
    empty and the client paginates via the chapters endpoint. `chapters`
    is always the full chapter index — cheap even for the largest guide
    (12 rows for study-in-india)."""

    specializations = serializers.SlugRelatedField(
        slug_field="name", many=True, read_only=True
    )
    chapters = GuideChapterIndexSerializer(many=True, read_only=True)
    sections = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()
    canonical_slug = serializers.CharField(source="slug", read_only=True)

    class Meta:
        model = CareerGuide
        fields = (
            "slug", "canonical_slug", "title", "blurb", "audience", "stage",
            "stage_label", "stage_order", "accent", "cover_url", "glance",
            "specializations", "class_levels", "reading_minutes",
            "view_count", "chapters", "sections", "seo_title", "seo_description",
        )

    def get_sections(self, obj):
        inline = self.context.get("inline_sections")
        if not inline:
            return []
        return GuideSectionSerializer(obj.sections.all(), many=True).data

    def get_cover_url(self, obj):
        request = self.context.get("request")
        if not obj.cover:
            return None
        return request.build_absolute_uri(obj.cover.url) if request else obj.cover.url


class CareerGuideAdminSerializer(serializers.ModelSerializer):
    """Staff CRUD — full field set, writable."""

    class Meta:
        model = CareerGuide
        fields = (
            "id", "slug", "title", "blurb", "audience", "stage", "stage_label",
            "stage_order", "accent", "cover", "legacy_slugs", "glance",
            "specializations", "class_levels", "tags", "order", "seo_title",
            "seo_description", "status", "publish_at", "reading_minutes",
            "view_count", "created_at", "updated_at",
        )
        read_only_fields = ("reading_minutes", "view_count", "created_at", "updated_at")
