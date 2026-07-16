# PLACEMENT: backend/content/admin_serializers.py
#
# Full read/write serializers for the staff-only CMS admin API
# (content/admin_views.py). Companion to serializers.py's curated,
# read-only public serializers — these expose the full writable field set
# so the React admin UI can drive complete CRUD.
#
# `tags` is accepted as a plain list of tag-name strings (not a nested M2M
# writer) — the admin ViewSets pop it out of validated_data and
# get_or_create() ContentTag rows in perform_create/perform_update,
# mirroring forum/moderation_views.py's tag handling
# (`Tag.objects.get_or_create(name=name.lower())` then `.tags.add(tag)`).
# It MUST be popped before `serializer.save()`: Django resolves "tags" as a
# real M2M relation on the model regardless of how the serializer declares
# it, so if left in validated_data, ModelSerializer.create()/update() would
# try `instance.tags.set([<tag-name strings>])` and blow up trying to treat
# tag names as ContentTag primary keys.

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem, ShowcaseCourse,
)


class TagNamesField(serializers.ListField):
    """Read/write bridge between a ContentTag M2M and a plain list of
    tag-name strings.

    A bare `ListField` can't do this: on read, DRF fetches the model
    attribute (`instance.tags`, the M2M manager) and hands it to
    `to_representation()` unchanged — the manager isn't iterable, so the
    default `ListField.to_representation` blows up with
    `TypeError: 'ManyRelatedManager' object is not iterable`. Overriding
    `to_representation()` to call `.all()` first fixes read, while write
    keeps ListField's normal `to_internal_value` (accepts a JSON list of
    strings, e.g. ["ncert", "class-9"]).

    This field is never assigned to the model directly — `tags` isn't a
    plain attribute a ModelSerializer can write, so the ViewSet pops it out
    of `validated_data` before `serializer.save()` and syncs the M2M itself
    (see `_sync_tags()` in admin_views.py).
    """

    child = serializers.CharField()

    def to_representation(self, data):
        return [tag.name for tag in data.all()]


class FullCleanMixin:
    """Runs Model.full_clean() (custom `clean()` + constraint validation)
    during serializer validation, so:
      * Announcement's `clean()` (ends_at must be after starts_at)
      * ShowcaseCourse's `clean()` (stars <= 5, categories must be a list)
      * BlogPost's conditional UniqueConstraint on
        (class_level, subject, chapter_number) — DRF's automatic
        unique-together validators skip constraints that have a
        `condition`, so without this the violation would only surface as
        an IntegrityError at save() time (a 500).
    all come back as ordinary DRF 400s instead of an uncaught
    ValidationError/IntegrityError at save time.

    `full_clean_exclude` lets a subclass skip specific fields in the
    clean_fields()/validate_unique()/validate_constraints() passes (the
    model's own custom `clean()` always still runs regardless — Django's
    full_clean() never lets `exclude` suppress that part).
    """

    full_clean_exclude = ()

    def validate(self, attrs):
        model = self.Meta.model
        instance = self.instance if self.instance is not None else model()

        concrete_fields = {
            f.name for f in model._meta.get_fields()
            if getattr(f, "concrete", False) and not f.many_to_many
        }
        for field, value in attrs.items():
            if field in concrete_fields:
                setattr(instance, field, value)

        exclude = list(self.full_clean_exclude)
        if "slug" in concrete_fields and not getattr(instance, "slug", None):
            # Blank slugs are filled in by the model's own save(); nothing to
            # validate yet, and validate_unique() would false-positive on
            # blank="" collisions before that auto-generation runs.
            exclude.append("slug")

        try:
            instance.full_clean(exclude=exclude)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict")
                else {"non_field_errors": exc.messages}
            )
        return attrs


# ── Blog ──────────────────────────────────────────────────────────

class BlogPostAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    tags = TagNamesField(
        required=False, help_text='Tag names, e.g. ["ncert", "class-9"].',
    )
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = [
            "id", "title", "slug", "class_level", "subject", "chapter_number",
            "excerpt", "cover", "body_html", "trusted_html", "tags",
            "author", "author_name", "is_featured", "seo_title",
            "seo_description", "reading_minutes", "view_count",
            "status", "publish_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "reading_minutes", "view_count", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "slug": {"required": False},
            "author": {"read_only": True},
        }

    def get_author_name(self, obj):
        if not obj.author:
            return ""
        full = getattr(obj.author, "get_full_name", lambda: "")() or ""
        return full or getattr(obj.author, "username", "") or ""


# ── Current affairs ───────────────────────────────────────────────

class CurrentAffairAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    tags = TagNamesField(
        required=False, help_text='Tag names, e.g. ["budget", "economy"].',
    )

    class Meta:
        model = CurrentAffair
        fields = [
            "id", "title", "slug", "affair_date", "category", "summary",
            "body_html", "source_name", "source_url", "tags",
            "status", "publish_at", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"slug": {"required": False}}


# ── FAQ / announcements / showcase / tags ────────────────────────

class FAQItemAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = FAQItem
        fields = ["id", "page", "question", "answer_html", "order", "is_active"]


class AnnouncementAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = [
            "id", "message", "link_url", "link_label", "level",
            "starts_at", "ends_at", "is_active", "order",
        ]


class ShowcaseCourseAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    # `categories` is `JSONField(default=list)` *without* `blank=True` on
    # the model (unlike the sibling `link_state = JSONField(default=dict,
    # blank=True)`) — so its own default value ([]) fails Model.clean_fields()'s
    # blank check ("This field cannot be blank."), which would 400 on every
    # create that doesn't explicitly pass a non-empty categories list (this
    # would affect Django admin saves too, since ModelForm._post_clean() also
    # calls full_clean()). Excluded here rather than editing models.py, which
    # is out of scope; ShowcaseCourse.clean()'s own `isinstance(..., list)`
    # check still runs regardless and still catches genuinely malformed input.
    full_clean_exclude = ("categories",)

    class Meta:
        model = ShowcaseCourse
        fields = [
            "id", "title", "level_label", "ribbon", "stars", "review_count",
            "fact_line", "price_label", "tutor_name", "is_explore_card",
            "categories", "gradient_css", "image", "image_url", "icon",
            "link_path", "link_state", "order", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ContentTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentTag
        fields = ["id", "name", "slug"]
        read_only_fields = ["slug"]
