"""Read/write plumbing for ContentChapterTag, shared by every taggable model.

One place, deliberately. Five models across four apps expose the same
`chapter_tags[]` / `chapter_note` / `no_specific_chapter` contract, and the
dedupe rule in particular ("a typed label that matches an existing chapter
name resolves to that chapter") has to behave identically on all of them or
the same input produces a chapter on one screen and reuses one on another.

THE ADDITIVE INVARIANT
──────────────────────
Writing tags also keeps the legacy per-model `chapter` FK populated (see
`primary_chapter`), because that FK is still what authorization-adjacent code
and every legacy read path use. Tags are the richer view alongside it, never
a replacement — until a later phase retires the FK after an audit.
"""

import json

from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from .models import Chapter
from .models_chapter_tags import ContentChapterTag
from .services import find_chapter_by_title, next_chapter_order


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------

def tags_for(instance):
    """Every tag on `instance`, ordered. Use `prefetch_chapter_tags` on the
    queryset when serializing a list, or this is a query per row."""
    return (
        ContentChapterTag.objects
        .filter(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
        )
        .select_related("chapter")
    )


def serialize_tags(instance):
    """`chapter_tags` payload for one instance.

    Reads a `_prefetched_chapter_tags` attribute when a caller has populated
    one (see `attach_chapter_tags`), so a list endpoint costs two queries
    rather than one per row.
    """
    cached = getattr(instance, "_prefetched_chapter_tags", None)
    tags = cached if cached is not None else tags_for(instance)
    return [
        {
            "chapter_id": str(t.chapter_id) if t.chapter_id else None,
            "label": t.label,
            "is_custom": True if t.chapter_id is None else t.chapter.is_custom,
            "order": t.order,
        }
        for t in tags
    ]


def attach_chapter_tags(instances):
    """Bulk-load tags onto `instances`, two queries total.

    ContentChapterTag is a generic relation, so Django's own
    prefetch_related() can't span it from an arbitrary queryset without a
    GenericRelation declared on every model. Doing it here avoids adding one
    to five models in four apps.
    """
    instances = list(instances)
    if not instances:
        return instances

    content_type = ContentType.objects.get_for_model(instances[0])
    by_object = {}
    for tag in (
        ContentChapterTag.objects
        .filter(content_type=content_type,
                object_id__in=[i.pk for i in instances])
        .select_related("chapter")
    ):
        by_object.setdefault(tag.object_id, []).append(tag)

    for instance in instances:
        instance._prefetched_chapter_tags = by_object.get(instance.pk, [])
    return instances


# ---------------------------------------------------------------------------
# WRITE
# ---------------------------------------------------------------------------

def validate_tag_payload(tags, no_specific_chapter):
    """Enforce the two structural rules before anything is resolved.

    Zero tags is VALID — this never blocks a save for being empty.
    """
    if no_specific_chapter and tags:
        raise serializers.ValidationError({
            "no_specific_chapter": (
                "Choose either 'no specific chapter' or one or more chapters, "
                "not both."
            )
        })


def resolve_tags(subject, tags, teacher=None, save_to_course=False):
    """Turn raw `[{chapter_id, label, is_custom}]` into (chapter, label) pairs.

    Resolution order per entry:
      1. `chapter_id` given  → that chapter, which MUST belong to `subject`.
         Rejecting a foreign chapter here is what stops a tag being used to
         attach content to a subject the teacher has no claim to.
      2. a label matching an existing chapter of this subject
         (case-insensitively, via the shared `find_chapter_by_title`) → that
         chapter. This is the dedupe rule: typing "trigonometry" when
         "Trigonometry" exists selects it rather than forking a duplicate.
      3. `save_to_course` → create a real Chapter (is_custom, attributed,
         appended to the end of the subject's order) and point the tag at it.
      4. otherwise → a free-text tag, chapter=None.

    Deduped, preserving first-seen order, so the same chapter or label sent
    twice yields one tag instead of tripping the unique constraint.
    """
    resolved = []
    seen = set()

    for index, entry in enumerate(tags):
        chapter = None
        label = ""

        raw_id = entry.get("chapter_id")
        raw_label = (entry.get("label") or "").strip()

        if raw_id:
            chapter = Chapter.objects.filter(id=raw_id, subject=subject).first()
            if chapter is None:
                raise serializers.ValidationError({
                    "chapter_tags": (
                        f"Chapter {raw_id} is not part of {subject.name}."
                    )
                })
        elif raw_label:
            chapter = find_chapter_by_title(subject, raw_label)
            if chapter is None and save_to_course:
                chapter = Chapter.objects.create(
                    subject=subject,
                    title=raw_label,
                    order=next_chapter_order(subject),
                    is_custom=True,
                    created_by=teacher,
                )
            if chapter is None:
                label = raw_label
        else:
            # Neither an id nor a label: nothing to tag. Skipped rather than
            # rejected, so a UI sending a trailing blank row still saves.
            continue

        key = ("chapter", chapter.id) if chapter else ("label", label.lower())
        if key in seen:
            continue
        seen.add(key)
        resolved.append((chapter, label, len(resolved)))

    return resolved


def primary_chapter(resolved):
    """Which chapter the legacy per-model `chapter` FK should hold.

    THE ADDITIVE INVARIANT. The first resolved chapter, or None when the
    content is tagged only with free text (or not at all).

    With exactly one chapter this is that chapter, which is what the spec
    requires. With SEVERAL it is the first rather than None, deliberately:
    leaving the FK null would drop the content out of every legacy
    chapter-filtered read (materials' per-chapter listing, the teacher
    coverage rollups), so a teacher adding a second chapter would watch the
    content disappear from screens that used to show it.
    """
    for chapter, _label, _order in resolved:
        if chapter is not None:
            return chapter
    return None


def set_tags(instance, resolved):
    """Replace `instance`'s tags with `resolved`. Idempotent."""
    content_type = ContentType.objects.get_for_model(instance)
    ContentChapterTag.objects.filter(
        content_type=content_type, object_id=instance.pk,
    ).delete()

    if not resolved:
        return

    ContentChapterTag.objects.bulk_create([
        ContentChapterTag(
            content_type=content_type,
            object_id=instance.pk,
            chapter=chapter,
            custom_label=label,
            order=order,
        )
        for chapter, label, order in resolved
    ])


# ---------------------------------------------------------------------------
# SERIALIZER MIXIN
# ---------------------------------------------------------------------------

class ChapterTagWriteMixin:
    """Adds `chapter_tags[]` + `save_chapters_to_course` to a ModelSerializer.

    `chapter_note` and `no_specific_chapter` are real model fields, so they
    only need listing in `Meta.fields`; these two are not.

    Subclasses must expose a `chapter_tag_subject(validated_data, instance)`
    hook returning the Subject the tags belong to, because each model reaches
    it differently.

    THE LEGACY SHIMS STAY. This mixin does not touch `chapter_id` or
    `custom_chapter` handling; the three live teacher screens still send those
    and each serializer keeps resolving them exactly as before. When a caller
    sends neither tags nor a legacy key, nothing about the existing chapter is
    changed.
    """

    def to_internal_value(self, data):
        """Decode a JSON-encoded `chapter_tags` sent over multipart.

        These endpoints accept file uploads, so real clients POST multipart —
        and multipart has no way to express a list of objects. A client with
        files therefore sends `chapter_tags` as a JSON *string*, which
        ListField(DictField) would reject. Decoding it here, before field
        validation, lets one payload shape work over both parsers.

        A malformed string is left exactly as it came in so ListField raises
        its own clear error, rather than being silently swallowed into "no
        tags" — a teacher who picked chapters must not be told the save
        succeeded with none.
        """
        raw = data.get("chapter_tags") if hasattr(data, "get") else None
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, list):
                # A plain dict, not QueryDict.copy(): QueryDict stores every
                # value as a list and get() returns only the LAST element, so
                # assigning a list of tag dicts into one would silently
                # collapse to the final tag.
                data = {**data.dict(), "chapter_tags": decoded} \
                    if hasattr(data, "dict") else {**data,
                                                   "chapter_tags": decoded}
        return super().to_internal_value(data)

    def pop_chapter_tag_input(self, attrs):
        """Pull the tag keys out of `attrs` and validate the structural rules.

        Returns (tags, save_to_course, present) where `present` is False when
        the caller said nothing about tags at all — which must leave existing
        tags untouched rather than clearing them.
        """
        present = "chapter_tags" in attrs
        tags = attrs.pop("chapter_tags", None) or []
        save_to_course = attrs.pop("save_chapters_to_course", False)

        no_specific = attrs.get("no_specific_chapter")
        if no_specific is None and self.instance is not None:
            no_specific = self.instance.no_specific_chapter
        validate_tag_payload(tags, bool(no_specific))

        return tags, bool(save_to_course), present

    def apply_chapter_tags(self, instance, subject, tags, save_to_course,
                           present):
        """Resolve and write tags, and keep the legacy `chapter` FK in step."""
        if not present:
            return instance

        teacher = getattr(self.context.get("request"), "user", None)
        resolved = resolve_tags(
            subject, tags, teacher=teacher, save_to_course=save_to_course,
        )
        set_tags(instance, resolved)

        # The additive invariant — see primary_chapter()'s docstring.
        chapter = primary_chapter(resolved)
        if instance.chapter_id != (chapter.id if chapter else None):
            instance.chapter = chapter
            instance.save(update_fields=["chapter"])
        return instance
