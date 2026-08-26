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

import os

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .blocks import validate_blocks, validate_theme
from .models import (
    Announcement, BlogPost, ContentImage, ContentTag, CurrentAffair,
    FAQItem, HomeContentBlock, HomeFloater, HomeListItem, HomeSectionOrder,
    ShowcaseCourse,
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
      * ShowcaseCourse's `clean()` (categories must be a list)
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
            "excerpt", "cover", "body_html", "body_blocks", "body_theme",
            "body_html_source", "trusted_html",
            "tags", "author", "author_name", "is_featured", "seo_title",
            "seo_description", "reading_minutes", "view_count",
            "status", "publish_at", "created_at", "updated_at",
            "locale", "translation_group",
        ]
        read_only_fields = [
            "body_html_source", "reading_minutes", "view_count",
            "created_at", "updated_at",
            # translation_group is server-assigned (default on create, or
            # copied explicitly by BlogPostAdminViewSet.duplicate_translation
            # — never accepted from a client payload, so a request can't
            # graft itself onto an arbitrary existing translation group).
            "translation_group",
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

    # Strict on write (unknown block type / non-hex theme value -> 400),
    # mirroring counseling/guide_serializers.py's validate_blocks(). Nothing
    # runs on read — an older backend build tolerates a post saved by a
    # newer frontend.
    def validate_body_blocks(self, value):
        return validate_blocks(value)

    def validate_body_theme(self, value):
        return validate_theme(value)


# ── Editor-uploaded images (rich-text body content) ────────────────

class ContentImageAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentImage
        fields = [
            "id", "file", "alt_text", "title", "width", "height",
            "focal_x", "focal_y", "uploaded_by", "created_at",
        ]
        read_only_fields = ["id", "width", "height", "uploaded_by", "created_at"]

    def create(self, validated_data):
        if not validated_data.get("title"):
            validated_data["title"] = os.path.splitext(
                os.path.basename(validated_data["file"].name)
            )[0]
        return super().create(validated_data)


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
        fields = ["id", "page", "question", "answer_html", "order", "status"]


class AnnouncementAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = [
            "id", "message", "link_url", "link_label", "level",
            "starts_at", "ends_at", "status", "order",
        ]


def _is_competitive(course):
    """A competitive course, by the discriminator the live surfaces use.

    Checks BOTH signals because they can disagree: `kind` is written on create
    and read by almost nothing, while the nav menu and catalog key on a linked
    CourseCategory whose group is "competitive". create_competitive_courses
    skips the category link (with a warning) when the categories were never
    seeded, which yields a COACHING course with no group — so keying on the
    group alone would miss exactly the courses most likely to be misfiled.
    """
    if getattr(course, "kind", None) == "COACHING":
        return True
    return course.categories.filter(group="competitive").exists()


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
    course_title = serializers.SerializerMethodField()

    def get_course_title(self, obj):
        return obj.course.title if obj.course_id else None

    class Meta:
        model = ShowcaseCourse
        fields = [
            "id", "title", "level_label", "ribbon",
            "fact_line", "price_label", "tutor_name", "is_explore_card",
            "categories", "gradient_css", "image", "image_url", "icon",
            "link_path", "link_state", "course", "course_title", "board",
            "order", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        # When linked to a real course, link_path/link_state are derived
        # server-side rather than trusting the manual link_state JSON textarea
        # (which previously had no relationship to what was actually linked).
        if "course" in attrs and attrs["course"] is not None:
            course = attrs["course"]
            attrs["link_path"] = "/courses"
            if course.board:
                attrs["link_state"] = {
                    "selectedBoardGroup": course.board.board_type.lower(),
                    # The SLUG, not the lowercased name. The catalog resolves
                    # this with `boards.find(b => b.slug === value)`, and the
                    # two only coincide for single-word boards: "BSE Odisha"
                    # lowercases to "bse odisha" and never matches its slug
                    # "bseodisha", so the deep link silently fell through to
                    # the default board.
                    "selectedBoard": course.board.slug,
                }
            elif _is_competitive(course):
                # Was `{}` — an empty state dropped the visitor on the catalog
                # with no filter, and until the catalog gained a competitive
                # axis it could not have shown the course at all. Send them to
                # that axis; Courses.jsx resolves this group specially because
                # "competitive" is a category group, not a board_type.
                attrs["link_state"] = {"selectedBoardGroup": "competitive"}
            else:
                attrs["link_state"] = {}
        return super().validate(attrs)


class ContentTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentTag
        fields = ["id", "name", "slug"]
        read_only_fields = ["slug"]


# ── Homepage content ───────────────────────────────────────────────

class ResolvedImageMixin:
    """Resolves the same read-only `img` the public serializers expose.

    The admin screens need it to render a thumbnail of what is currently
    saved: the editor writes `image` (an upload) or `image_url` (a link), but
    neither is much use as a preview src on its own — `image` is a relative
    path and only one of the two is ever set. Without this, the admin form's
    `previewUrl={row?.img}` was always undefined and no existing image ever
    showed up next to the upload control.

    Note each serializer must still declare `img = SerializerMethodField()`
    itself — DRF's SerializerMetaclass only harvests declared fields from
    bases that are Serializers, so a field defined on a plain mixin like this
    one is silently ignored and you get "Field name `img` is not valid for
    model ..." from build_unknown_field. Only the resolver lives here.
    """

    def get_img(self, obj):
        if obj.image:
            request = self.context.get("request")
            url = obj.image.url
            return request.build_absolute_uri(url) if request else url
        return obj.image_url or ""


class HomeContentBlockAdminSerializer(
    ResolvedImageMixin, FullCleanMixin, serializers.ModelSerializer
):
    img = serializers.SerializerMethodField()

    class Meta:
        model = HomeContentBlock
        fields = [
            "id", "section", "eyebrow", "heading", "heading_secondary",
            "subhead", "body", "cta_primary_label", "cta_primary_href",
            "cta_secondary_label", "cta_secondary_href", "image", "image_url",
            "img", "extra", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class HomeListItemAdminSerializer(
    ResolvedImageMixin, FullCleanMixin, serializers.ModelSerializer
):
    img = serializers.SerializerMethodField()

    class Meta:
        model = HomeListItem
        fields = [
            "id", "section", "variant", "icon", "title", "subtitle", "body",
            "pills", "stat_text", "cta_label", "cta_href", "tint", "image",
            "image_url", "img", "order", "status", "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class HomeFloaterAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    class Meta:
        model = HomeFloater
        fields = [
            "id", "section", "slot", "icon", "label", "sublabel", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class HomeSectionOrderAdminSerializer(FullCleanMixin, serializers.ModelSerializer):
    class Meta:
        model = HomeSectionOrder
        fields = ["id", "section", "order", "is_visible", "created_at", "updated_at"]
        read_only_fields = ["section", "created_at", "updated_at"]
