# PLACEMENT: backend/content/serializers.py

from rest_framework import serializers

from .models import (
    Announcement, BlogPost, CurrentAffair, FAQItem, ShowcaseCourse,
)


class TagListField(serializers.RelatedField):
    def to_representation(self, value):
        return value.name


def _absolute(request, url):
    if not url:
        return None
    return request.build_absolute_uri(url) if request else url


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
            "reading_minutes", "is_featured", "publish_at",
        ]

    def get_thumbnail(self, obj):
        return _absolute(
            self.context.get("request"),
            obj.cover.url if obj.cover else "",
        )


class BlogPostDetailSerializer(BlogPostListSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta(BlogPostListSerializer.Meta):
        fields = BlogPostListSerializer.Meta.fields + [
            "body_html", "seo_title", "seo_description",
            "author_name", "view_count", "updated_at",
        ]

    def get_author_name(self, obj):
        if not obj.author:
            return ""
        full = getattr(obj.author, "get_full_name", lambda: "")() or ""
        return full or getattr(obj.author, "username", "") or ""


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
        fields = ["id", "message", "link_url", "link_label", "level"]


class ShowcaseCourseSerializer(serializers.ModelSerializer):
    img = serializers.SerializerMethodField()

    class Meta:
        model = ShowcaseCourse
        fields = [
            "id", "title", "level_label", "ribbon", "stars", "review_count",
            "fact_line", "price_label", "tutor_name", "is_explore_card",
            "categories", "gradient_css", "img", "icon",
            "link_path", "link_state", "course", "order",
        ]

    def get_img(self, obj):
        if obj.image:
            return _absolute(self.context.get("request"), obj.image.url)
        return obj.image_url or ""
