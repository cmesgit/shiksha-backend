import uuid
import os
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from courses.models_chapter_tags import (
    chapter_note_field,
    no_specific_chapter_field,
)


# ==========================================
# ASSIGNMENT MODEL
# ==========================================

class Assignment(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    # THE curriculum anchor, and the authorization source of truth.
    #
    # Authorization for an assignment (who may edit it, who may see it, which
    # course/board it belongs to) used to be derived by walking
    # chapter.subject.course — the staffing triangle in serializers.py, the
    # _teacher_scope() queryset in views.py, the media gate in
    # config/media_security.py, the teacher rollups in dashboard/views.py.
    # That was only safe while `chapter` was a required FK.
    #
    # `chapter` is now optional (a teacher may legitimately save an assignment
    # with no chapter, or several chapters, via courses.ContentChapterTag), so
    # deriving authorization through it would mean a NULL chapter silently
    # produced "no subject" and therefore no staffing check. Authorization
    # must never depend on a nullable field, so it hangs off this instead.
    #
    # Note `batch` below cannot serve this purpose either: it is nullable by
    # design (SET_NULL, so deleting a batch demotes content rather than
    # destroying it), and legacy rows have none.
    #
    # This mirrors what Quiz, LiveSession and SessionRecording already do —
    # all three have carried a non-null `subject` plus an optional `chapter`
    # from the start. Assignment and StudyMaterial were the outliers.
    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="assignments",
        db_index=True,
    )

    # Curriculum placement within the subject. OPTIONAL: see `subject` above.
    #
    # SET_NULL, not CASCADE. Teacher-typed chapter names became real
    # courses.Chapter rows when custom chapters shipped, which meant an admin
    # tidying a subject's syllabus would silently delete every assignment
    # filed under the chapter they removed — including student submissions,
    # which cascade off Assignment. Losing the curriculum label is an
    # acceptable outcome of deleting a chapter; losing the graded work is not.
    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        db_index=True
    )

    # Delivery scope. NULL = visible to every batch of the course (legacy
    # rows); set = this batch only. Due dates are cohort-relative, so new
    # assignments should always carry a batch (enforced in the serializer,
    # not the DB, so legacy rows stay valid). SET_NULL: deleting a batch
    # demotes its assignments to course-wide instead of destroying them.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
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

    # What a submission is graded out of. A plain default rather than a
    # required field so existing assignments (created before grading existed)
    # stay valid without a backfill.
    max_marks = models.PositiveIntegerField(default=100)

    # Draft gate, mirroring Quiz.is_published.
    #
    # Until this existed, activity/signals.assignment_created fired on the
    # post_save of a brand-new row, so every student was notified the instant
    # a teacher hit save — there was no way to stage an assignment, fix a
    # typo, or attach a file before it went out.
    #
    # DEFAULT TRUE, deliberately: every existing assignment is live, and a
    # default of False would make the whole back catalogue vanish from
    # students' lists the moment this migration ran. Staging is opt-in — a
    # teacher chooses to save a draft. Publishing later (False → True) fires
    # the notification then, via the same pre_save/post_save transition pair
    # quizzes use.
    is_published = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Unpublished assignments are drafts: invisible to students "
                  "and silent. Publishing notifies the class.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # --- Flexible chapter tagging (courses.models_chapter_tags) ---
    # The rich multi-chapter placement lives in ContentChapterTag, keyed on
    # (content_type, object_id). These two are the scalar companions; see
    # courses/models_chapter_tags.py for what each one means and why
    # no_specific_chapter is not the same state as "no tags".
    chapter_note = chapter_note_field()
    no_specific_chapter = no_specific_chapter_field()

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
            models.Index(fields=["subject"]),
            models.Index(fields=["due_date"]),
            models.Index(fields=["batch", "due_date"]),
        ]

    def save(self, *args, **kwargs):
        # `subject` is NOT NULL because authorization hangs off it, but for
        # years the only thing callers passed was `chapter` — the subject was
        # implied by it. Deriving it here keeps every existing writer (the
        # create serializer, seed_demo_data, admin inlines, test fixtures)
        # correct without each having to learn about the new column, and makes
        # it impossible to end up with a row whose authorization anchor is
        # unset.
        #
        # Only fills a MISSING subject. It deliberately does not "correct" a
        # subject that disagrees with the chapter: silently rewriting one of
        # two conflicting values would hide the bug that produced them. The
        # serializers enforce that a chapter stays inside the assignment's own
        # subject instead.
        if self.subject_id is None and self.chapter_id is not None:
            self.subject_id = self.chapter.subject_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.chapter or self.subject})"

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

    # The ACCOUNT that submitted (kept for audit; matches the
    # user/learner_profile dual-keying already used by enrollments).
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assignment_submissions",
        db_index=True
    )

    # The LEARNER PROFILE the submission belongs to. Without this, two
    # children on one account overwrite each other's uploads (the old
    # unique(assignment, student) rule). Nullable only for legacy rows —
    # backfill with `manage.py backfill_activity_profiles`.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="assignment_submissions",
        null=True,
        blank=True,
    )

    submitted_file = models.FileField(upload_to="assignments/submissions/")

    # NOT auto_now. This field decides the On-time/Late chip the student sees
    # and the teacher grades against, and auto_now rewrites it on ANY save
    # that doesn't pass update_fields — a management command, an admin edit,
    # a future field added to the grading save — silently converting an
    # on-time submission into a late one long after the fact. It is a fact
    # about an event, so it's stamped at the event: SubmitAssignmentView sets
    # it explicitly, including on a resubmission, where re-stamping IS
    # correct. default (not auto_now_add) so that explicit set is honoured.
    submitted_at = models.DateTimeField(default=timezone.now, db_index=True)

    # ── Grading ──────────────────────────────────────────────────────
    # Nullable/blank: an ungraded submission has none of these set. Marks are
    # validated against the assignment's own max_marks in the grading view,
    # not here, since that requires the related Assignment instance.
    marks_obtained = models.PositiveIntegerField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    graded_at = models.DateTimeField(null=True, blank=True)
    graded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="graded_assignment_submissions",
    )

    class Meta:
        constraints = [
            # One submission per LEARNER PROFILE per assignment.
            models.UniqueConstraint(
                fields=["assignment", "learner_profile"],
                condition=Q(learner_profile__isnull=False),
                name="uniq_submission_per_profile",
            ),
            # Legacy rows (pre-profile) keep the old account-level rule
            # until backfill_activity_profiles runs.
            models.UniqueConstraint(
                fields=["assignment", "student"],
                condition=Q(learner_profile__isnull=True),
                name="uniq_submission_legacy_account",
            ),
        ]
        indexes = [
            models.Index(fields=["assignment", "student"]),
            models.Index(fields=["assignment", "learner_profile"]),
            models.Index(fields=["submitted_at"]),
        ]
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.student} → {self.assignment.title}"
