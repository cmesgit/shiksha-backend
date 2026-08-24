"""Generic chapter tagging for every piece of teacher-authored content.

WHY THIS EXISTS
───────────────
Curriculum placement used to be a single required FK per model
(``Assignment.chapter``, ``StudyMaterial.chapter``, …). That forced a teacher
to pick exactly one chapter from the curated syllabus even when the truth was
"this covers three chapters", "this is a revision sheet spanning the whole
term", or "this doesn't map to a chapter at all". This table lets one piece of
content carry zero, one, or many chapters — plus free-text labels for things
the syllabus has no row for.

ADDITIVE, NOT A REPLACEMENT
───────────────────────────
The per-model ``chapter`` FKs stay, and they remain what authorization and
every existing read path use. This table is the richer view alongside them.
Whenever a write resolves to exactly ONE chapter, the serializers keep the
legacy ``chapter`` FK populated so nothing downstream changes behaviour.
Dropping the old FKs is a later phase, and only after a real audit.

WHY ``object_id`` IS A UUIDField
────────────────────────────────
Every one of the five taggable models — Assignment, StudyMaterial,
LiveSession, Quiz, SessionRecording — declares
``id = models.UUIDField(primary_key=True, default=uuid.uuid4)``. The usual
generic-relation recipe of ``object_id = PositiveIntegerField()`` cannot store
any of them: it would raise on every single write. A ``CharField`` would work
too but gives up type checking and native uuid indexing for a flexibility
nothing here needs, since there is no integer-PK model in the taggable set.
If a future taggable model uses an integer PK, this column has to widen to a
CharField at that point — there is no way to make one column serve both.
"""

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class ContentChapterTag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    # See the module docstring: every taggable model has a UUID primary key.
    object_id = models.UUIDField(db_index=True)
    content_object = GenericForeignKey("content_type", "object_id")

    # NULL means this tag is free text only (``custom_label`` carries it).
    # SET_NULL rather than CASCADE: an admin tidying the syllabus must not
    # silently delete a teacher's tag — it degrades to the label instead.
    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="content_tags",
    )

    # Only meaningful when ``chapter`` is NULL. A label that matches an
    # existing chapter name (case-insensitively) is resolved to that chapter at
    # write time instead of being stored here, so this never shadows a real
    # chapter.
    custom_label = models.CharField(max_length=120, blank=True)

    order = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["chapter"]),
        ]
        constraints = [
            # The spec's unique_together. NOTE THE SQL NULL CAVEAT: because
            # NULLs compare as distinct, this does NOT constrain rows where
            # chapter IS NULL — i.e. it cannot stop two identical free-text
            # labels on one object. The partial constraint below closes exactly
            # that hole, and the serializers dedupe before writing either way.
            models.UniqueConstraint(
                fields=["content_type", "object_id", "chapter", "custom_label"],
                name="uniq_chapter_tag_per_object",
            ),
            models.UniqueConstraint(
                fields=["content_type", "object_id", "custom_label"],
                condition=models.Q(chapter__isnull=True),
                name="uniq_freetext_chapter_tag_per_object",
            ),
        ]

    def __str__(self):
        label = self.chapter.title if self.chapter_id else self.custom_label
        return f"{self.content_type_id}:{self.object_id} → {label}"

    @property
    def label(self):
        """What a client should display for this tag."""
        if self.chapter_id:
            return self.chapter.title
        return self.custom_label
