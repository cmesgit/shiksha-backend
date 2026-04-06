import uuid
from django.db import models
from django.conf import settings
from imagekit.models import ProcessedImageField
from imagekit.processors import ResizeToFill


class Course(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

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
        return f"{self.course} → {self.name}"   # ✅ improved


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
