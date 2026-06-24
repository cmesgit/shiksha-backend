import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


# -------------------------------------------------------
# 1️⃣ QUIZ
# -------------------------------------------------------

class Quiz(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="quizzes",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_quizzes",
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    due_date = models.DateTimeField(null=True, blank=True)
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)

    total_marks = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["subject"]),
            models.Index(fields=["is_published"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"


# -------------------------------------------------------
# 2️⃣ QUESTION
# -------------------------------------------------------

class Question(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="questions",
    )

    text = models.TextField()
    marks = models.PositiveIntegerField(default=1)
    order = models.PositiveIntegerField(default=0)
    explanation = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        indexes = [
            models.Index(fields=["quiz", "order"]),
        ]

    def __str__(self):
        return f"Question {self.order} - {self.quiz.title}"


# -------------------------------------------------------
# 3️⃣ CHOICE
# -------------------------------------------------------

class Choice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="choices",
    )

    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"Choice for {self.question.id}"


# -------------------------------------------------------
# 4️⃣ QUIZ ATTEMPT
# -------------------------------------------------------

class QuizAttempt(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_SUBMITTED = "SUBMITTED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUBMITTED, "Submitted"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="attempts",
    )

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
    )

    attempt_number = models.PositiveIntegerField(default=1)

    score = models.FloatField(default=0)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["student", "quiz"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["quiz", "student", "attempt_number"],
                name="unique_attempt_per_number"
            )
        ]

    def __str__(self):
        return f"{self.student.email} → {self.quiz.title}"


# -------------------------------------------------------
# 5️⃣ STUDENT ANSWER
# -------------------------------------------------------

class StudentAnswer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    attempt = models.ForeignKey(
        QuizAttempt,
        on_delete=models.CASCADE,
        related_name="answers",
    )

    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
    )

    selected_choice = models.ForeignKey(
        Choice,
        on_delete=models.CASCADE,
    )

    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"Answer {self.question.id} - {self.attempt.student.email}"
