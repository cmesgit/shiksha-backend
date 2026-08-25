import uuid
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from courses.models_chapter_tags import (
    chapter_note_field,
    no_specific_chapter_field,
)


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
    #
    # ⚠ NOT dropped by Phase 10, deliberately. Nothing READS it any more — the
    # practice endpoints and S3 all go through `chapter_tags` below — but it is
    # still WRITTEN by courses/chapter_tags.py's additive invariant
    # (`instance.chapter = primary_chapter(...)`, chapter_tags.py:318), and that
    # write is shared by five models across four apps. Assignment.chapter is
    # required and its staffing-triangle check derives subject/course through
    # it, so the mixin cannot simply stop writing the FK. Removing this column
    # means making that shared write model-aware first — a change to shared
    # infrastructure, not a cleanup. See migration 0029's docstring.
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
    # long as the teacher has it assigned — gated by is_assigned alone).
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)

    # ── The two reveal fields, and why there are two ──────────────────────
    #
    # These are NOT duplicates. They answer different questions and are
    # combined in exactly one place — `answers_revealed_for()` below, which
    # is the only thing any view may call. Never re-derive the rule inline.
    #
    # `reveal_answers_after` (pre-existing, an ATTEMPT BUDGET, "how many"):
    # bounds how many of a student's own attempts get the full answer-key
    # review (correct_choice + explanation in QuizResultView), so an
    # unlimited retake can't be used to read the key and then resubmit for a
    # free 100%. An anti-cheat quota, not a display preference.
    reveal_answers_after = models.PositiveIntegerField(default=1)

    # `reveal_answers` (Phase 4, a TIMING MODE, "when"): at what point in the
    # flow the learner is allowed to see correctness at all. Nothing in
    # `reveal_answers_after` can express this — it has no notion of
    # during-vs-after the paper, and no way to say "never".
    #
    #   after_each   — instant per-question feedback while attempting
    #                  (practice mode; gates CheckAnswerView)
    #   after_submit — nothing until the paper is submitted, then the normal
    #                  end-of-attempt review (mock mode)
    #   never        — no answer key, ever, on any attempt
    #
    # DEFAULTS: the spec wants `after_each` for practice and `after_submit`
    # for mock. A per-quiz-type default cannot be a plain field default, so
    # the field default is `after_each` (the practice value, and the
    # behaviour every pre-Phase-4 practice quiz already had) and the mock
    # default is applied at CREATION time by QuizCreateSerializer when the
    # client doesn't send the field. Existing mock rows were backfilled to
    # `after_submit` by migration 0026 — behaviourally a no-op (CheckAnswerView
    # already refuses mock quizzes), but it keeps the stored value honest.
    REVEAL_AFTER_EACH = "after_each"
    REVEAL_AFTER_SUBMIT = "after_submit"
    REVEAL_NEVER = "never"
    REVEAL_CHOICES = [
        (REVEAL_AFTER_EACH, "After each question"),
        (REVEAL_AFTER_SUBMIT, "After the paper is submitted"),
        (REVEAL_NEVER, "Never"),
    ]
    reveal_answers = models.CharField(
        max_length=16, choices=REVEAL_CHOICES, default=REVEAL_AFTER_EACH,
    )

    # ── Mock-test settings (Phase 4) ──────────────────────────────────────
    # All defaulted so every pre-existing practice quiz keeps its behaviour
    # with no data migration: 0 penalty, unlimited attempts, no shuffle.

    # Marks deducted per WRONG answer. Applied by QuizSubmitSerializer ONLY
    # when quiz_type == "mock" — a practice attempt must never subtract, no
    # matter what is stored here (a quiz switched mock→practice keeps its
    # configured value so switching back doesn't lose it). Blank/unanswered
    # questions are never penalised. UI offers 0, 0.25, 0.33, 0.5, 1.
    negative_marks_per_wrong = models.DecimalField(
        max_digits=4, decimal_places=2, default=0,
    )

    # Attempt QUOTA. NULL = unlimited (practice); 1 = single-attempt (mock).
    # This is a quota, deliberately separate from StartQuizView's
    # `new_attempt: true` flag, which is an INTENT check (it stops a stray
    # Back-button re-mount from burning an attempt). See StartQuizView for
    # how the two compose. NULL default means no existing quiz gains a limit.
    max_attempts = models.PositiveSmallIntegerField(null=True, blank=True)

    # Per-attempt question order randomisation (mock papers). Read by the
    # attempt screen; scoring is order-independent.
    shuffle_questions = models.BooleanField(default=False)

    quiz_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default=TYPE_MOCK,
    )

    total_marks = models.PositiveIntegerField(default=0)

    # THE student-visibility gate. Teacher-controlled: this, plus batch
    # membership (see `batches`), is the whole rule. Deliberately independent
    # of `review_status` — a teacher must never need an admin to make their
    # own quiz live for their own class.
    #
    # Phase 1 backfilled it from the old `is_published` gate, NOT from
    # review_status, because is_published is what every student queryset
    # actually filtered on and so was the only faithful source for "who could
    # see this yesterday". Phase 10 then dropped is_published (migration
    # 0029); see quizzes/migrations/0020 for that backfill's reasoning.
    is_assigned = models.BooleanField(default=False, db_index=True)

    # --- Flexible chapter tagging (courses.models_chapter_tags) ---
    # The rich multi-chapter placement lives in ContentChapterTag, keyed on
    # (content_type, object_id). These two are the scalar companions; see
    # courses/models_chapter_tags.py for what each one means and why
    # no_specific_chapter is not the same state as "no tags".
    chapter_note = chapter_note_field()
    no_specific_chapter = no_specific_chapter_field()

    # Declared on Quiz ONLY, deliberately breaking chapter_tags.py's
    # "don't add one to five models in four apps" rule. attach_chapter_tags()
    # is enough for serializing a page of rows, but it cannot be JOINED, and
    # the Phase 8 practice endpoints need exactly that: they aggregate
    # question supply and graded accuracy *grouped by chapter* across the
    # whole bank. Those currently go through the legacy `chapter` FK, which
    # Phase 10 wants to drop — this is what lets them move off it without
    # turning one aggregate into N queries.
    #
    # No DB column and no data: GenericRelation is a reverse-join declaration,
    # so its migration is a no-op state change.
    chapter_tags = GenericRelation(
        "courses.ContentChapterTag",
        content_type_field="content_type",
        object_id_field="object_id",
        related_query_name="quiz",
    )

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
            models.Index(fields=["review_status"]),
            # The is_published / (batch, is_published) pair went with the
            # column in Phase 10. (batch, is_assigned) is the replacement and
            # already existed — every student queryset filters on is_assigned.
            models.Index(fields=["batch", "is_assigned"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"

    @property
    def is_editable(self):
        """Teacher may only add/edit questions while a quiz hasn't been
        submitted for admin verification (or after it was sent back)."""
        return self.review_status in (self.REVIEW_DRAFT, self.REVIEW_REJECTED)

    # ── The one place the two reveal fields combine ───────────────────────

    @property
    def instant_feedback_enabled(self):
        """May CheckAnswerView hand back correctness mid-attempt?

        Practice mode AND `reveal_answers == after_each`. A practice quiz set
        to `after_submit`/`never` is a legitimate configuration (untimed,
        unlimited retries, but no peeking) and must not leak the key
        per-question.
        """
        return (
            self.quiz_type == self.TYPE_PRACTICE
            and self.reveal_answers == self.REVEAL_AFTER_EACH
        )

    def answers_revealed_for(self, attempt_number):
        """Is the end-of-attempt answer key visible on this attempt?

        Composes the timing mode with the attempt budget — see the long
        comment on the two fields above. `never` wins outright; otherwise
        practice always reveals after submit (that mode's whole point) and a
        mock reveals only within its `reveal_answers_after` budget.
        """
        if self.reveal_answers == self.REVEAL_NEVER:
            return False
        if self.quiz_type == self.TYPE_PRACTICE:
            return True
        return attempt_number <= self.reveal_answers_after


# -------------------------------------------------------
# 1️⃣b QUIZ SECTION  (mock tests)
# -------------------------------------------------------

class QuizSection(models.Model):
    """A named group of questions inside a mock paper ("Section A · Objective").

    Only mock tests use these. A question with `section=NULL` belongs to the
    flat list, which is what every practice quiz (and every pre-Phase-4 quiz)
    looks like — so this model is purely additive.

    PK is a UUID, matching every other model in this codebase (Quiz,
    Question, Choice, QuizAttempt, StudentAnswer all do the same). An
    implicit integer PK here would be the only one, and would leak
    guessable/enumerable section ids into the teacher API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    quiz = models.ForeignKey(
        Quiz, on_delete=models.CASCADE, related_name="sections",
    )
    name = models.CharField(max_length=80)
    order = models.PositiveSmallIntegerField(default=0)
    instructions = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order", "name"]
        indexes = [models.Index(fields=["quiz", "order"])]

    def __str__(self):
        return f"{self.name} — {self.quiz.title}"


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

    # Which mock-paper section this question sits in. NULL = the flat list,
    # i.e. every practice quiz and every pre-Phase-4 quiz.
    #
    # SET_NULL is the whole design, not a default: deleting a section must
    # NOT delete the teacher's questions. They fall back to the flat list —
    # "merge this section's questions into the main list" comes for free, and
    # a mis-click on a section's delete button can never destroy question
    # content. (CASCADE here would silently take the questions, their
    # choices and their answer key with it.)
    section = models.ForeignKey(
        "QuizSection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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


# -------------------------------------------------------
# 6️⃣ CHAPTER PRACTICE (Phase 8)
# -------------------------------------------------------
# A practice set is NOT a Quiz and a practice answer is NOT a StudentAnswer,
# deliberately. Two reasons, both load-bearing:
#
#   1. "Practice must not pollute graded analytics" (BUILD_GUIDE Phase 8
#      item 4). A boolean on QuizAttempt would put that guarantee in the hands
#      of every present and future aggregation site remembering to exclude it.
#      There are at least six such sites today, and the `total_marks`
#      regression showed exactly how quietly one missed spot fails here.
#      Separate tables make the guarantee structural: graded queries cannot
#      reach this data even by mistake.
#
#   2. A practice set draws its questions from the shared bank, which spans
#      MANY quizzes. QuizAttempt.quiz is a single FK and cannot represent
#      that. Synthesising a Quiz per session would litter every teacher's
#      list with system rows and duplicate questions that then drift from
#      the originals a teacher keeps editing.
#
# Practice is ungraded by design: no score, no marks, no attempt quota. The
# only thing recorded is what was answered and whether it was right, which is
# what the weak-area report needs and nothing more.

class PracticeSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="practice_sessions",
    )
    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.CASCADE,
        related_name="practice_sessions",
    )
    # The questions served, in the order served. Bank questions are shared, so
    # this is M2M rather than ownership — deleting a session must never touch
    # the teacher's question.
    questions = models.ManyToManyField(Question, related_name="practice_sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["learner_profile", "chapter"])]

    def __str__(self):
        return f"Practice {self.chapter_id} · {self.learner_profile_id}"


class PracticeAnswer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PracticeSession, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="practice_answers")
    selected_choice = models.ForeignKey(
        Choice, on_delete=models.CASCADE, null=True, blank=True,
        related_name="practice_answers",
    )
    is_correct = models.BooleanField(default=False)
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One answer per question per session — retrying re-draws a NEW
        # session rather than overwriting history, so the record of what a
        # learner knew when stays intact.
        constraints = [
            models.UniqueConstraint(
                fields=["session", "question"], name="unique_practice_answer"),
        ]

    def __str__(self):
        return f"PracticeAnswer {self.question_id}"
