# PLACEMENT: backend/content/serializers.py

from rest_framework import serializers

from .models import (
    Announcement, BlogPost, CurrentAffair, FAQItem, HomeContentBlock,
    HomeFloater, HomeListItem, HomeSectionOrder, ShowcaseCourse,
)


class TagListField(serializers.RelatedField):
    def to_representation(self, value):
        return value.name


def _absolute(request, url):
    if not url:
        return None
    return request.build_absolute_uri(url) if request else url


def _blog_path(locale, slug):
    # English stays unprefixed (every existing /blogs/<slug> link keeps
    # working); only non-English locales get a /blogs/<locale>/ segment.
    return f"/blogs/{slug}" if locale == "en" else f"/blogs/{locale}/{slug}"


# ── Blog ──────────────────────────────────────────────────────────

class BlogPostListSerializer(serializers.ModelSerializer):
    tags = TagListField(many=True, read_only=True)
    thumbnail = serializers.SerializerMethodField()
    category = serializers.CharField(source="get_subject_display", read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            "id", "slug", "title", "excerpt", "class_level", "subject",
            "category", "chapter_number", "thumbnail", "tags",
            "reading_minutes", "is_featured", "publish_at", "locale",
        ]

    def get_thumbnail(self, obj):
        return _absolute(
            self.context.get("request"),
            obj.cover.url if obj.cover else "",
        )


class BlogPostDetailSerializer(BlogPostListSerializer):
    author_name = serializers.SerializerMethodField()
    translations = serializers.SerializerMethodField()
    # Set by BlogPostDetailView as a plain instance attribute (not a model
    # field) when it serves the English fallback for a locale with no
    # translation yet — absent/False for a normal same-locale hit.
    is_fallback_locale = serializers.SerializerMethodField()

    class Meta(BlogPostListSerializer.Meta):
        fields = BlogPostListSerializer.Meta.fields + [
            # body_blocks/body_theme are authoritative when body_blocks is
            # non-empty — BlogDetail.jsx renders from them directly and only
            # falls back to body_html for legacy (pre-block-editor) posts.
            "body_html", "body_blocks", "body_theme",
            "seo_title", "seo_description",
            "author_name", "view_count", "updated_at",
            "translations", "is_fallback_locale",
        ]

    def get_author_name(self, obj):
        if not obj.author:
            return ""
        full = getattr(obj.author, "get_full_name", lambda: "")() or ""
        return full or getattr(obj.author, "username", "") or ""

    def get_translations(self, obj):
        # Sibling locale -> slug/path map, so the frontend locale switcher
        # never has to guess a sibling URL by string-manipulating the slug
        # (a Hindi translation can legitimately diverge from its English
        # sibling's slug — the convention is to reuse it, not a guarantee).
        siblings = BlogPost.objects.filter(
            translation_group=obj.translation_group
        ).exclude(pk=obj.pk).values("locale", "slug")
        result = [{
            "locale": obj.locale,
            "slug": obj.slug,
            "path": _blog_path(obj.locale, obj.slug),
        }]
        result += [
            {"locale": s["locale"], "slug": s["slug"], "path": _blog_path(s["locale"], s["slug"])}
            for s in siblings
        ]
        return result

    def get_is_fallback_locale(self, obj):
        return bool(getattr(obj, "is_fallback_locale", False))


# ── Current affairs ───────────────────────────────────────────────

class CurrentAffairListSerializer(serializers.ModelSerializer):
    tags = TagListField(many=True, read_only=True)
    category_label = serializers.CharField(
        source="get_category_display", read_only=True
    )

    class Meta:
        model = CurrentAffair
        fields = [
            "id", "slug", "title", "summary", "affair_date",
            "category", "category_label", "source_name", "tags",
        ]


class CurrentAffairDetailSerializer(CurrentAffairListSerializer):
    class Meta(CurrentAffairListSerializer.Meta):
        fields = CurrentAffairListSerializer.Meta.fields + [
            "body_html", "source_url", "updated_at",
        ]


# ── FAQ / announcements / showcase ────────────────────────────────

class FAQItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = FAQItem
        fields = ["id", "page", "question", "answer_html", "order"]


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        # updated_at is what lets the navbar key its "dismissed" flag on
        # (id, updated_at) instead of id alone. Without it, editing a live
        # announcement kept it hidden forever for anyone who had already
        # dismissed the previous wording — the row id never changes.
        fields = ["id", "message", "link_url", "link_label", "level", "updated_at"]


class ShowcaseCourseSerializer(serializers.ModelSerializer):
    img = serializers.SerializerMethodField()

    class Meta:
        model = ShowcaseCourse
        fields = [
            "id", "title", "level_label", "ribbon", "stars", "review_count",
            "fact_line", "price_label", "tutor_name", "is_explore_card",
            "categories", "gradient_css", "img", "icon",
            "link_path", "link_state", "course", "board", "order",
        ]

    def get_img(self, obj):
        if obj.image:
            return _absolute(self.context.get("request"), obj.image.url)
        return obj.image_url or ""


# ── Homepage content ───────────────────────────────────────────────

class HomeContentBlockSerializer(serializers.ModelSerializer):
    img = serializers.SerializerMethodField()

    class Meta:
        model = HomeContentBlock
        fields = [
            "id", "section", "eyebrow", "heading", "heading_secondary",
            "subhead", "body", "cta_primary_label", "cta_primary_href",
            "cta_secondary_label", "cta_secondary_href", "img", "extra",
        ]

    def get_img(self, obj):
        if obj.image:
            return _absolute(self.context.get("request"), obj.image.url)
        return obj.image_url or ""


class HomeListItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomeListItem
        fields = [
            "id", "section", "variant", "icon", "title", "subtitle", "body",
            "pills", "stat_text", "cta_label", "cta_href", "tint", "order",
        ]


class HomeFloaterSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomeFloater
        fields = ["id", "section", "slot", "icon", "label", "sublabel"]


class HomeSectionOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomeSectionOrder
        fields = ["section", "order", "is_visible"]
