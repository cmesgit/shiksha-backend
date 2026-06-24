import uuid
import os
from django.db import models
from django.conf import settings
from django.utils import timezone


# ==========================================
# ASSIGNMENT MODEL
# ==========================================

class Assignment(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.CASCADE,
        related_name="assignments",
        db_index=True
    )

    title = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    # Legacy single-file field — kept for backwards compat.
    # New uploads go through AssignmentFile.
    attachment = models.FileField(
        upload_to="assignments/files/",
        null=True,
        blank=True
    )

    due_date = models.DateTimeField(db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # -------------------------------------------------------
    # Idempotency key — teacher frontend generates a random
    # UUID per "new assignment" form session and sends it as
    # X-Idempotency-Key header (or body field). We store it
    # and enforce uniqueness so double-submits are no-ops.
    # -------------------------------------------------------
    idempotency_key = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text=(
            "Client-supplied UUID that prevents duplicate creation "
            "on accidental double-submit. Optional but recommended."
        ),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["chapter"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.chapter})"

    @property
    def is_expired(self):
        return timezone.now() > self.due_date


# ==========================================
# ASSIGNMENT FILE MODEL  (multi-file support)
# ==========================================

def assignment_file_upload_path(instance, filename):
    return os.path.join(
        "assignments", "files", str(instance.assignment_id), filename
    )


class AssignmentFile(models.Model):
    """
    Stores one or more teacher-uploaded files per assignment.
    Replaces the single `attachment` field for new uploads while
    keeping the legacy field for old data.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name="files",
        db_index=True,
    )

    file = models.FileField(upload_to=assignment_file_upload_path)

    original_filename = models.CharField(max_length=255, blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self):
        return f"{self.original_filename} → {self.assignment.title}"

    def save(self, *args, **kwargs):
        if not self.original_filename and self.file:
            self.original_filename = os.path.basename(self.file.name)
        super().save(*args, **kwargs)


# ==========================================
# ASSIGNMENT SUBMISSION MODEL
# ==========================================

class AssignmentSubmission(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name="submissions",
        db_index=True
    )

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assignment_submissions",
        db_index=True
    )

    submitted_file = models.FileField(upload_to="assignments/submissions/")

    submitted_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "student"],
                name="unique_assignment_submission"
            )
        ]
        indexes = [
            models.Index(fields=["assignment", "student"]),
            models.Index(fields=["submitted_at"]),
        ]
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.student} → {self.assignment.title}"
