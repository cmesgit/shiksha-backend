import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class BatchChapterProgress(models.Model):
    """Per-batch teaching progress for a single chapter.

    This is the piece the old design was missing. ``Chapter.is_covered`` is a
    single flag shared by every batch of a course, so two batches that move at
    different speeds cannot have different progress. This model makes coverage
    a fact about *(batch × chapter)* instead:

        - A teacher ticks a chapter covered **for their batch**.
        - Every student in that batch sees the same state.
        - A different batch of the same course is unaffected.
        - Teachers can attach a short note / indicator per chapter per batch
          ("revised in doubt class", "moved to next week", etc.).

    ``Chapter`` stays the canonical syllabus; this table is the join that
    records who covered what, for whom, and when.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.CASCADE,
        related_name="chapter_progress",
    )

    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.CASCADE,
        related_name="batch_progress",
    )

    is_covered = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Marked by a teacher once this chapter has been taught to this batch.",
    )
    covered_at = models.DateTimeField(null=True, blank=True)

    # Free-text teacher note / indicator, shown to the batch's students.
    note = models.TextField(blank=True, default="")

    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marked_batch_chapters",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "chapter"],
                name="unique_progress_per_batch_chapter",
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "is_covered"]),
        ]

    def __str__(self):
        state = "covered" if self.is_covered else "pending"
        return f"{self.batch.code} · {self.chapter.title} [{state}]"

    def clean(self):
        # A chapter's course must match its batch's course. Enforced here so
        # the admin and any serializer .is_valid() path catch mismatches; the
        # write endpoints validate this explicitly too.
        if (
            self.batch_id
            and self.chapter_id
            and self.chapter.subject.course_id != self.batch.course_id
        ):
            raise ValidationError(
                "Chapter does not belong to the same course as this batch."
            )
