import uuid
from django.db import models
from django.conf import settings
from imagekit.models import ProcessedImageField
from imagekit.processors import ResizeToFill


class Course(models.Model):

    # Which engine delivers this course. ACADEMIC = the cohort engine
    # (batches, teaching assignments); COACHING (JEE/NEET/...) reuses the
    # same engine later with board/stream/class_level left NULL.
    KIND_ACADEMIC = "ACADEMIC"
    KIND_COACHING = "COACHING"
    KIND_CHOICES = [
        (KIND_ACADEMIC, "Academy"),
        (KIND_COACHING, "Coaching"),
    ]

    # Lifecycle gate for the buy flow: only PUBLISHED courses (with an open
    # batch) appear in the student catalog. ARCHIVED hides from purchase but
    # keeps existing enrollments working.
    STATUS_DRAFT = "DRAFT"
    STATUS_PUBLISHED = "PUBLISHED"
    STATUS_ARCHIVED = "ARCHIVED"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default=KIND_ACADEMIC,
        db_index=True,
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT,
        db_index=True,
    )
    # Explicit class level (6..12) instead of encoding it in the title.
    # NULL for coaching courses and legacy rows the backfill couldn't parse.
    class_level = models.PositiveSmallIntegerField(
        null=True, blank=True, db_index=True,
    )

    price = models.PositiveIntegerField(default=0, help_text="Price in paise (₹1 = 100 paise)")

    subscription_duration_days = models.PositiveIntegerField(
        default=30,
        help_text="How many days of access a single approved enrollment grants (default = 1 month)",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    board = models.ForeignKey(
        "Board",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="courses"
    )
    stream = models.ForeignKey(
        "Stream",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="courses"
    )

    def __str__(self):
        base = self.title

        if self.stream:
            base += f" - {self.stream.name}"

        if self.board:
            base += f" [{self.board.name}]"

        return base

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["title", "stream", "board"],
                name="unique_course_per_stream_board"
            )
        ]


class Subject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="subjects",
    )

    name = models.CharField(max_length=100)
    image = ProcessedImageField(
        upload_to="subjects/images/",
        processors=[ResizeToFill(800, 400)],
        format="WEBP",          # smaller than JPG/PNG
        options={"quality": 80},
        blank=True,
        null=True
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "name"],
                name="unique_subject_per_course"
            )
        ]

    def __str__(self):
        return f"{self.course} → {self.name}"


class Chapter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="chapters",
    )

    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    # --- Teacher-driven coverage (one shared state per course) ---
    # A teacher ticks this once the chapter has been taught. Every enrolled
    # student sees the same value, including students who join later.
    is_covered = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Marked by a teacher once this chapter has been taught.",
    )
    covered_at = models.DateTimeField(null=True, blank=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marked_chapters",
    )

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "title"],
                name="unique_chapter_per_subject"
            )
        ]

    def __str__(self):
        return self.title


class CourseDetail(models.Model):
    course = models.OneToOneField(
        Course,
        on_delete=models.CASCADE,
        related_name="details"
    )

    level = models.CharField(max_length=50)
    duration_weeks = models.PositiveIntegerField()
    syllabus = models.TextField(blank=True)

    language = models.CharField(max_length=50, default="English")
    requirements = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Details of {self.course.title}"


class SubjectTeacher(models.Model):
    ROLE_PRIMARY = "PRIMARY"
    ROLE_ASSISTANT = "ASSISTANT"

    ROLE_CHOICES = [
        (ROLE_PRIMARY, "Primary Teacher"),
        (ROLE_ASSISTANT, "Assistant"),
    ]

    subject = models.ForeignKey(
        "Subject",
        on_delete=models.CASCADE,
        related_name="subject_teachers"
    )

    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subject_assignments"
    )

    display_role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_PRIMARY
    )

    order = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "teacher"],
                name="unique_teacher_per_subject"
            )
        ]

    def __str__(self):
        return f"{self.subject.name} → {self.teacher.email}"


class Board(models.Model):
    TYPE_STATE = "STATE"
    TYPE_CENTRAL = "CENTRAL"

    TYPE_CHOICES = [
        (TYPE_STATE, "State"),
        (TYPE_CENTRAL, "Central"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=100, unique=True, db_index=True)
    board_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES
    )
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive boards render as 'Coming Soon' / dormant on the public site.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["board_type", "name"]
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.board_type})"


class Stream(models.Model):
    STREAM_CHOICES = [
        ("SCIENCE", "Science"),
        ("COMMERCE", "Commerce"),
        ("ARTS", "Arts"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=20, choices=STREAM_CHOICES, unique=True)

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# NEW: Batch / cohort
#
# Promotes the old free-text Enrollment.batch_code into a real, queryable
# entity. A batch belongs to one course (e.g. "Batch 2026", "A13", "A15").
# Filtering, counting and capacity all become trivial and consistent.
# ---------------------------------------------------------------------------
class Batch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="batches",
    )

    # Human-readable label shown in dashboards: "Batch 2026", "Morning A13"
    name = models.CharField(max_length=100)

    # Short operational code, unique within a course: "A13", "A15", "2026"
    code = models.CharField(max_length=20)

    # Academic session year, e.g. 2025 / 2026. Optional but handy for filtering.
    year = models.PositiveIntegerField(null=True, blank=True)

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max active students; leave blank for unlimited.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive batches are hidden from new enrollments.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "code"],
                name="unique_batch_code_per_course",
            ),
        ]
        indexes = [
            models.Index(fields=["course", "is_active"]),
            models.Index(fields=["year"]),
        ]

    def __str__(self):
        return f"{self.course.title} — {self.name} ({self.code})"

    @property
    def seats_taken(self):
        # "ACTIVE" mirrors Enrollment.STATUS_ACTIVE (kept as a literal to avoid
        # a cross-app import at module load time).
        return self.enrollments.filter(status="ACTIVE").count()

    @property
    def is_full(self):
        return self.capacity is not None and self.seats_taken >= self.capacity


# Delivery-plane teaching roster: who teaches which subject *in which batch*.
# Replaces SubjectTeacher (course-wide, batch-blind) as the source of truth;
# SubjectTeacher stays during the migration window as a read-only fallback
# and is dropped in the final cleanup phase.
class TeachingAssignment(models.Model):
    ROLE_PRIMARY = "PRIMARY"
    ROLE_ASSISTANT = "ASSISTANT"
    ROLE_SUBSTITUTE = "SUBSTITUTE"
    ROLE_CHOICES = [
        (ROLE_PRIMARY, "Primary teacher"),
        (ROLE_ASSISTANT, "Assistant"),
        (ROLE_SUBSTITUTE, "Substitute"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    batch = models.ForeignKey(
        Batch,
        on_delete=models.CASCADE,
        related_name="teaching_assignments",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="teaching_assignments",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="teaching_assignments",
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_PRIMARY)
    order = models.PositiveIntegerField(default=1)

    # Audit trail instead of hard deletes: when a teacher leaves or is
    # substituted, END the row (is_active=False, ended_at=now) and add a new
    # one. "Who taught batch A13 maths in July" stays answerable.
    is_active = models.BooleanField(default=True, db_index=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teaching_assignments_made",
    )

    class Meta:
        ordering = ["order"]
        constraints = [
            # A teacher appears once per (batch, subject) among ACTIVE rows.
            models.UniqueConstraint(
                fields=["batch", "subject", "teacher"],
                condition=models.Q(is_active=True),
                name="uniq_active_teacher_per_batch_subject",
            ),
            # Exactly one active PRIMARY per (batch, subject).
            models.UniqueConstraint(
                fields=["batch", "subject"],
                condition=models.Q(is_active=True, role="PRIMARY"),
                name="uniq_active_primary_per_batch_subject",
            ),
        ]
        indexes = [
            models.Index(fields=["teacher", "is_active"]),
            models.Index(fields=["batch", "subject", "is_active"]),
        ]

    def clean(self):
        # Guard the triangle: the subject must belong to the batch's course.
        if self.subject.course_id != self.batch.course_id:
            from django.core.exceptions import ValidationError
            raise ValidationError("Subject and batch belong to different courses.")

    def __str__(self):
        return f"{self.batch.code} · {self.subject.name} → {self.teacher.email} ({self.role})"
