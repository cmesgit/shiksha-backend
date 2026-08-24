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
    #
    # QuizCreateSerializer now sets this (batch-aware, same as assignments).
    # StartQuizView/QuizDetailView/SubmitQuizView (quizzes/views.py) gate on
    # it via _assert_learner_may_see_quiz(), the same
    # Q(batch__isnull=True) | Q(batch_id=<learner's batch for this course>)
    # rule materials/views.py's StudentSubjectMaterials uses.
    #
    # ⚠ LEGACY SINGLE-BATCH SHIM. Superseded by `batches` (M2M) below, but
    # still WRITTEN by QuizCreateSerializer and by the assign endpoint, and
    # still READ as a fallback by quizzes/visibility.py whenever `batches`
    # is empty. It cannot be dropped before Phase 10: any writer that sets
    # only this field (quiz create, duplicate) would otherwise produce a
    # quiz with an empty `batches` set, which the new rule reads as
    # "course-wide" — silently leaking a batch-scoped quiz to every batch.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quizzes",
    )

    # Delivery scope, multi-batch. EMPTY = every batch of the course, which
    # preserves exactly what `batch IS NULL` meant before this field existed;
    # non-empty = only these batches. Additive: `batch` above stays as the
    # single-batch shim for old clients (see its comment).
    #
    # Read through quizzes/visibility.py — never inline a `batches` filter.
    # The helper there uses Exists() subqueries rather than a join precisely
    # so that adding batch scoping to a queryset cannot multiply rows and
    # silently inflate a Count()/aggregate() computed alongside it.
    batches = models.ManyToManyField(
        "courses.Batch",
        blank=True,
        related_name="assigned_quizzes",
    )

    # Curriculum placement, same role Chapter plays for Assignment/StudyMaterial.
    # Optional: legacy quizzes and evergreen question banks may have none.
    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quizzes",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_quizzes",
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Quizzes never expire (product decision: a quiz stays attemptable for as
    # long as it's published, gated only by is_published / review_status).
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)

    # Retakes stay unlimited by design (see StartQuizView) — this instead
    # bounds how many of a student's own attempts get the full answer-key
    # review (correct_choice + explanation in QuizResultView), so a retake
    # can't be used to read the key and then resubmit for a free 100%.
    # Ignored for TYPE_PRACTICE, where instant per-question feedback is the
    # whole point of the mode.
    reveal_answers_after = models.PositiveIntegerField(default=1)

    quiz_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default=TYPE_MOCK,
    )

    total_marks = models.PositiveIntegerField(default=0)

    # ⚠ LEGACY. Was the student-visibility gate; `is_assigned` is now. Still
    # written (mirrored) by the assign endpoint and by AdminQuizReviewView so
    # old clients reading it keep working. Retires in Phase 10.
    is_published = models.BooleanField(default=False)

    # THE student-visibility gate. Teacher-controlled: this, plus batch
    # membership (see `batches`), is the whole rule. Deliberately independent
    # of `review_status` — a teacher must never need an admin to make their
    # own quiz live for their own class. Backfilled from `is_published`, NOT
    # from review_status: `is_published` is what every student queryset
    # actually filtered on, so it is the only faithful source for "who could
    # see this yesterday".
    is_assigned = models.BooleanField(default=False, db_index=True)

    # Informational only since Phase 1 — the admin's opinion of the questions,
    # shown on the teacher's card. MUST NOT gate student visibility.
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
            models.Index(fields=["batch", "is_assigned"]),
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

    # Provenance for the builder's per-question badge (AI-drafted, imported
    # from bulk-paste, pulled from the question bank) — purely informational,
    # never affects grading or visibility.
    SOURCE_MANUAL = "manual"
    SOURCE_AI = "ai"
    SOURCE_BANK = "bank"
    SOURCE_IMPORT = "import"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_AI, "AI-drafted"),
        (SOURCE_BANK, "From question bank"),
        (SOURCE_IMPORT, "Bulk import"),
    ]
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL,
    )

    # ── Phase 2: question-level site-bank state ─────────────────────────────
    # Independent axis from Quiz.review_status/is_assigned (see
    # quizzes/visibility.py's module docstring for that axis). A teacher's own
    # tests run regardless of any of this — bank_state only decides whether a
    # question is pulled into the shared ShikshaCom bank other teachers and
    # student chapter-practice draw on.
    BANK_STATE_PRIVATE = "private"
    BANK_STATE_SUGGESTED = "suggested"
    BANK_STATE_ACCEPTED = "accepted"
    BANK_STATE_CHANGES_REQUESTED = "changes_requested"
    BANK_STATE_CHOICES = [
        (BANK_STATE_PRIVATE, "Kept private"),
        (BANK_STATE_SUGGESTED, "Suggested — awaiting curation"),
        (BANK_STATE_ACCEPTED, "Accepted into the ShikshaCom bank"),
        (BANK_STATE_CHANGES_REQUESTED, "Admin asked for changes"),
    ]
    # NOTE: README's data-model table says CharField(16), but the longest
    # choice value ("changes_requested") is 17 characters and would not fit —
    # 20 gives headroom without changing any of the four choice strings.
    bank_state = models.CharField(
        max_length=20, choices=BANK_STATE_CHOICES, default=BANK_STATE_SUGGESTED,
    )

    # Teacher's per-question opt-out. True by default: everything a teacher
    # writes is auto-suggested to the bank unless they say otherwise (README
    # "Data model changes" §2). Enforced against bank_state in save() below —
    # this field is the only thing a teacher-facing path may write; the four
    # fields after it are admin-only (Phase 7).
    suggest_to_bank = models.BooleanField(default=True)

    # Written ONLY by the Phase 7 admin review endpoint. Never set from a
    # teacher-facing call site.
    bank_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="bank_reviewed_questions",
    )
    bank_reviewed_at = models.DateTimeField(null=True, blank=True)
    bank_feedback = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        indexes = [
            models.Index(fields=["quiz", "order"]),
            models.Index(fields=["topic"]),
            models.Index(fields=["difficulty"]),
            models.Index(fields=["bank_state"]),
        ]

    def __str__(self):
        return f"Question {self.order} - {self.quiz.title}"

    def save(self, *args, **kwargs):
        # THE invariant (README §Data model 2 / BUILD_GUIDE Phase 2 item 3):
        # suggest_to_bank=False must always force bank_state="private", and
        # this is enforced here — at the model level — rather than only in
        # the PATCH endpoint, precisely because Question.objects.create() is
        # called from half a dozen call sites (AddQuestionView,
        # BulkAddQuestionsView, BulkQuestionCreateSerializer, the bulk-replace
        # PUT) and every one of them must get the same guarantee for free.
        #
        # The opposite direction is deliberately NOT unconditional. If an
        # admin already reviewed this question (bank_state is "accepted" or
        # "changes_requested") and suggest_to_bank is still True, leave
        # bank_state alone. A teacher fixing a typo and re-saving the
        # question must not silently revert an admin's "accepted" back to
        # "suggested" — that would discard real curation work. Only a
        # genuinely fresh/unset/private row gets normalised to "suggested".
        if not self.suggest_to_bank:
            self.bank_state = self.BANK_STATE_PRIVATE
        elif self.bank_state not in (
            self.BANK_STATE_ACCEPTED, self.BANK_STATE_CHANGES_REQUESTED,
        ):
            self.bank_state = self.BANK_STATE_SUGGESTED
        super().save(*args, **kwargs)


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
