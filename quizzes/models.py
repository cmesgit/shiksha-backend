import uuid
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone


# -------------------------------------------------------
# 1️⃣ QUIZ
# -------------------------------------------------------

class Quiz(models.Model):
    # ── Quiz-taking mode ──────────────────────────────────────────────────
    # PRACTICE: untimed, one question at a time, instant feedback + streak.
    # MOCK: timed, full-paper palette navigation, graded only on submit.
    TYPE_PRACTICE = "practice"
    TYPE_MOCK = "mock"
    TYPE_CHOICES = [
        (TYPE_PRACTICE, "Practice — instant feedback"),
        (TYPE_MOCK, "Mock test — timed"),
    ]

    # ── Admin verification workflow ───────────────────────────────────────
    # DRAFT: teacher still editing, never shown to students.
    # PENDING: submitted by teacher, awaiting admin verification.
    # APPROVED: verified by admin — this is what makes a quiz live/published.
    # REJECTED: admin sent it back with a note; teacher can edit & resubmit.
    REVIEW_DRAFT = "draft"
    REVIEW_PENDING = "pending"
    REVIEW_APPROVED = "approved"
    REVIEW_REJECTED = "rejected"
    REVIEW_STATUS_CHOICES = [
        (REVIEW_DRAFT, "Draft"),
        (REVIEW_PENDING, "Pending review"),
        (REVIEW_APPROVED, "Approved"),
        (REVIEW_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="quizzes",
    )

    # Delivery scope. NULL = evergreen (practice quizzes / question banks,
    # visible to every batch of the course); set = scoped to one batch
    # (e.g. "Batch A13 weekly test"). SET_NULL: deleting a batch demotes
    # its quizzes to course-wide instead of destroying them.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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

    quiz_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default=TYPE_MOCK,
    )

    total_marks = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=False)

    review_status = models.CharField(
        max_length=10, choices=REVIEW_STATUS_CHOICES, default=REVIEW_DRAFT,
    )
    review_note = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="reviewed_quizzes",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    submitted_for_review_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["subject"]),
            models.Index(fields=["is_published"]),
            models.Index(fields=["review_status"]),
            models.Index(fields=["batch", "is_published"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"

    @property
    def is_editable(self):
        """Teacher may only add/edit questions while a quiz hasn't been
        submitted for admin verification (or after it was sent back)."""
        return self.review_status in (self.REVIEW_DRAFT, self.REVIEW_REJECTED)


# -------------------------------------------------------
# 2️⃣ QUESTION
# -------------------------------------------------------

class Question(models.Model):
    DIFFICULTY_EASY = "easy"
    DIFFICULTY_MEDIUM = "medium"
    DIFFICULTY_HARD = "hard"
    DIFFICULTY_CHOICES = [
        (DIFFICULTY_EASY, "Easy"),
        (DIFFICULTY_MEDIUM, "Medium"),
        (DIFFICULTY_HARD, "Hard"),
    ]

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

    # Used for the question bank filters and for per-topic / per-difficulty
    # analytics on the student results screen.
    topic = models.CharField(max_length=120, blank=True, default="")
    difficulty = models.CharField(
        max_length=10, choices=DIFFICULTY_CHOICES, default=DIFFICULTY_MEDIUM,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        indexes = [
            models.Index(fields=["quiz", "order"]),
            models.Index(fields=["topic"]),
            models.Index(fields=["difficulty"]),
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

    # The ACCOUNT that took the attempt (kept for audit; matches the
    # user/learner_profile dual-keying already used by enrollments).
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
    )

    # The LEARNER PROFILE the attempt belongs to. One account can hold
    # several learner profiles (parent + children); scores, attempt
    # numbering and resume logic are all per-profile. Nullable only for
    # legacy rows written before this field existed — backfill with
    # `manage.py backfill_activity_profiles`.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
        null=True,
        blank=True,
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
            models.Index(fields=["learner_profile", "quiz"]),
        ]
        constraints = [
            # Attempt numbering is per LEARNER PROFILE — two children on the
            # same account each get their own attempt 1, 2, 3…
            models.UniqueConstraint(
                fields=["quiz", "learner_profile", "attempt_number"],
                condition=Q(learner_profile__isnull=False),
                name="uniq_attempt_per_profile_number",
            ),
            # Legacy rows (pre-profile, learner_profile NULL) keep the old
            # account-level rule until backfill_activity_profiles runs.
            models.UniqueConstraint(
                fields=["quiz", "student", "attempt_number"],
                condition=Q(learner_profile__isnull=True),
                name="uniq_attempt_legacy_account_number",
            ),
        ]

    def __str__(self):
        who = self.learner_profile.display_name if self.learner_profile else self.student.email
        return f"{who} → {self.quiz.title}"


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

    # Analytics: dwell time on this question (mock "time per question" chart)
    # and whether the student flagged it via "mark for review" during a mock.
    time_spent_seconds = models.PositiveIntegerField(default=0)
    marked_for_review = models.BooleanField(default=False)

    def __str__(self):
        return f"Answer {self.question.id} - {self.attempt.student.email}"
