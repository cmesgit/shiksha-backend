# PLACEMENT: backend/backend/materials/models.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/materials/models.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# MaterialFile gains `uploaded_by` (nullable FK → User). Without it, the
# two-step upload flow (POST /files/upload/ → POST /materials/upload/ with
# file_ids) had no way to verify that the person attaching a temp file is the
# person who uploaded it — any authenticated user who learned a file's UUID
# could claim it, or re-parent a file already attached to someone else's
# material. Views now filter claims on (material IS NULL, uploaded_by = me).
#
# Migration required after deploying this file:
#   python manage.py makemigrations materials
#   python manage.py migrate materials
#
# The field is nullable so existing rows migrate cleanly; the views treat
# legacy NULL-uploader temp files as claimable by anyone (grandfathered).

import uuid
from django.db import models
from django.conf import settings
from courses.models import Chapter

from .validators import validate_material_file


class StudyMaterial(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # THE curriculum anchor, and the authorization source of truth. See the
    # long note on assignments.Assignment.subject — same reasoning, same
    # reason `batch` can't serve the purpose (nullable by design), and same
    # alignment with Quiz / LiveSession / SessionRecording, which have always
    # carried a non-null subject plus an optional chapter.
    #
    # The gate this protects is materials/views.py's teaches_subject() checks
    # and config/media_security.py's download authorization, both of which
    # used to walk chapter.subject.
    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="materials",
        db_index=True,
    )

    # Curriculum placement within the subject. OPTIONAL: a revision sheet may
    # span the whole term, or map to several chapters via
    # courses.ContentChapterTag, or to none at all.
    #
    # SET_NULL, not CASCADE — teacher-typed chapters are real Chapter rows, so
    # CASCADE meant an admin tidying the syllabus silently destroyed the
    # uploaded material (and its MaterialFile rows) filed under the chapter
    # they deleted. Same live data-loss path as Assignment.chapter had.
    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materials"
    )

    # Delivery scope. NULL (the default) = curriculum asset shared by every
    # batch of the course — write once, reuse across batches and years.
    # Set only for genuinely batch-specific handouts. SET_NULL: deleting a
    # batch demotes its materials to course-wide instead of destroying them.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materials",
    )

    title = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_materials"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # See the identical note on assignments.Assignment.save(): `subject` is
        # NOT NULL because authorization reads it, but historically callers
        # only passed `chapter`, which implies it. Derive rather than break
        # every existing writer — and never "correct" a subject that disagrees
        # with the chapter, which would hide the bug that produced the
        # disagreement.
        if self.subject_id is None and self.chapter_id is not None:
            self.subject_id = self.chapter.subject_id
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class MaterialFile(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    material = models.ForeignKey(
        StudyMaterial,
        on_delete=models.CASCADE,
        related_name="files",
        null=True,
        blank=True
    )

    # Who uploaded this file (set at temp-upload time). Used to stop other
    # users from claiming someone else's pending upload. Nullable for rows
    # created before this field existed.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_material_files",
    )

    file = models.FileField(
        upload_to="study_materials/",
        validators=[validate_material_file],
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    def filename(self):
        if not self.file:
            return "file"
        return self.file.name.split("/")[-1]

    def __str__(self):
        return self.filename()
