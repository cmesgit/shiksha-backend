import uuid
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
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

    # Delivery scope used to be a single `batch` FK here — the pre-Phase-2
    # shim. Phase 10 removed it (migration 0032); `batches` below is the only
    # scope there is. Two things to know if you go looking for it:
    #
    #   · quizzes/visibility.py lost its fallback clause with the column. An
    #     empty `batches` set now unambiguously means "every batch of the
    #     course", which is only safe because create and duplicate both
    #     populate the M2M (migration 0031 backfilled the stragglers). Any new
    #     writer MUST set `batches`, or its quiz silently goes course-wide.
    #   · the reverse accessor `batch.quizzes` is gone; query `assigned_quizzes`
    #     (the M2M's related_name) instead.

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

    # Curriculum placement used to be a `chapter` FK here. Phase 10 removed it
    # (migration 0030): placement lives ONLY in ContentChapterTag now, reached
    # through the `chapter_tags` GenericRelation declared further down.
    #
    # Two things went with it. The reverse accessor: anything that reached for
    # `chapter.quizzes` has to query ContentChapterTag instead. And the shared
    # additive invariant in courses/chapter_tags.py, which used to keep this FK
    # in step — it is now guarded by has_chapter_fk(), because the four other
    # taggable models still have theirs (Assignment's is required, with
    # authorization derived through it).

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
            # Phase 10 dropped is_published and then the `batch` shim, so the
            # is_published, (batch, is_published) and (batch, is_assigned)
            # indexes all went with their columns. `is_assigned` keeps its own
            # db_index (declared on the field) — it is what every student
            # queryset filters on, and batch scope is now an Exists() subquery
            # over the M2M's own indexed join table, not a column on Quiz.
            models.Index(fields=["is_assigned"]),
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

class QuestionTag(models.Model):
    """One optional classification facet on a bank question.

    Every axis the public Quiz Hub filters on — subject, exam, topic, and
    whatever comes next — is a ROW here rather than a column on Question.
    That is the whole point: adding "exam stage" or "paper" later is an
    INSERT, not a migration, which is what "tags stay optional as this
    scales" has to mean in practice.

    ``year`` is the deliberate exception and lives on Question as a real
    integer column — it is range-queried and sorted ("2019 onwards"), and a
    string tag would make that a text comparison.

    Two optional links keep this from forking a second vocabulary:

      * ``content_tag`` points at content.ContentTag, which Content Studio's
        Labels screen already manages (create / rename / merge). Its slug is
        unique and derived from the name, so "Biology" and "  biology  "
        cannot both exist — exactly the property wanted here.
      * ``course`` points at the real competitive-exam Course, so an "SSC
        CGL" tag and the catalog agree on what SSC CGL is. An exam is a
        Course in this codebase, never a free string.

    The presentation fields (icon / color / cover_image / display_order) and
    ``status`` are meaningful only for the subject and exam rails the hub
    renders; they stay NULL/blank for topic tags. They live here rather than
    in a separate table so the "a new facet is a row" property survives.
    """

    KIND_SUBJECT = "subject"
    KIND_EXAM = "exam"
    KIND_TOPIC = "topic"
    KIND_CUSTOM = "custom"
    KIND_CHOICES = [
        (KIND_SUBJECT, "Subject"),
        (KIND_EXAM, "Exam"),
        (KIND_TOPIC, "Topic"),
        (KIND_CUSTOM, "Custom"),
    ]

    # The rails the public hub renders. See effective_status() below — the
    # server, not the admin, has the last word on what a visitor sees.
    STATUS_LIVE = "live"
    STATUS_SOON = "soon"
    STATUS_HIDDEN = "hidden"
    STATUS_CHOICES = [
        (STATUS_LIVE, "Live — clickable, shows its question count"),
        (STATUS_SOON, "Soon — greyed out, not clickable"),
        (STATUS_HIDDEN, "Hidden — not rendered at all"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, db_index=True)
    label = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True)

    content_tag = models.ForeignKey(
        "content.ContentTag",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="question_tags",
        help_text="Optional link to the CMS label of the same name.",
    )
    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="question_tags",
        help_text="For kind='exam': the competitive-exam Course this names.",
    )

    # ── Presentation, for the subject / exam rails only ──────────────────
    status = models.CharField(
        max_length=8, choices=STATUS_CHOICES, default=STATUS_SOON,
        help_text=(
            "What a visitor sees. Defaults to 'soon' so a newly created "
            "subject never appears clickable before it has questions."
        ),
    )
    display_order = models.PositiveSmallIntegerField(default=0)
    icon = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Sprite id from the hub's inline SVG sheet, e.g. 'qi-book'.",
    )
    color = models.CharField(
        max_length=9, blank=True, default="",
        help_text="Accent hex for the subject card, e.g. '#0F9D6B'.",
    )
    cover_image = models.ForeignKey(
        "content.ContentImage",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="question_tag_covers",
        help_text="Cover art for the recommendation cards, from the CMS library.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "display_order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "slug"], name="uniq_question_tag_per_kind"
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "status"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.label}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.label)
        super().save(*args, **kwargs)

    def effective_status(self, live_question_count):
        """What a visitor actually sees, given how many live questions exist.

        ``live`` is a FLOOR, not an override. An admin may force a rail to
        ``soon`` or ``hidden`` for any reason — content is thin, the subject
        is being retired, a paper is embargoed. An admin may NOT force
        ``live`` onto a facet with no questions behind it, because that
        renders a clickable chip that opens an empty grid, which is precisely
        the lie this page exists to avoid.

        The disagreement is not swallowed: the admin screens surface it as a
        warning ("set to live but has 0 questions — still showing as Soon")
        so a human sees why their setting did not take.
        """
        if self.status == self.STATUS_HIDDEN:
            return self.STATUS_HIDDEN
        if self.status == self.STATUS_LIVE and live_question_count > 0:
            return self.STATUS_LIVE
        return self.STATUS_SOON


class QuestionQuerySet(models.QuerySet):
    """Home for query-time rules that must never drift from being re-derived
    ad hoc at each call site — see publishable() below."""

    def publishable(self):
        """Questions safe to serve to the public Quiz Hub.

        This is intentionally STRICTER than "bank_state == accepted" alone,
        because "accepted" and "actually renderable" are not the same thing
        on this data:

          * Prod today has 10 accepted questions but only 11 WITH an
            explanation platform-wide (design_handoff_public_quiz_hub/
            README.md §5) — accepted rows with no explanation exist for
            real, and the whole reason the admin bank-question create
            endpoint leaves `explanation` optional is so previous-year
            imports can land before anyone has written one. A question with
            no explanation must not reach a learner who is about to be told
            "here's why you got that wrong" and shown nothing.
          * >=2 choices and exactly-one-is_correct are enforced at
            CREATE time by the serializers (this file's admin write
            serializer, and the older QuestionCreateSerializer). This
            re-asserts the same invariant at READ time so a row that
            reached a degenerate shape via some other path (a bad direct
            DB write, a half-finished migration/backfill, choices deleted
            out from under an otherwise-fine question) can't slip through
            silently — better an accepted-looking question quietly does not
            appear than one appears with zero or several correct answers.

        distinct=True on both counts because two annotations both joining
        the same `choices` relation would otherwise multiply rows against
        each other the way CLAUDE.md's "don't count joined relations without
        distinct=True" note warns about generally in this codebase.
        """
        return (
            self.exclude(explanation="")
            .filter(bank_state=self.model.BANK_STATE_ACCEPTED)
            .annotate(
                _choice_count=models.Count("choices", distinct=True),
                _correct_choice_count=models.Count(
                    "choices",
                    filter=Q(choices__is_correct=True),
                    distinct=True,
                ),
            )
            .filter(_choice_count__gte=2, _correct_choice_count=1)
        )


class Question(models.Model):
    # Custom manager ONLY to add .publishable() (see QuestionQuerySet above).
    # `QuestionQuerySet.as_manager()` keeps every default manager method
    # (create/filter/get/...) working exactly as before — this is additive,
    # not a behaviour change to any existing `Question.objects.*` call site.
    objects = QuestionQuerySet.as_manager()

    DIFFICULTY_EASY = "easy"
    DIFFICULTY_MEDIUM = "medium"
    DIFFICULTY_HARD = "hard"
    DIFFICULTY_CHOICES = [
        (DIFFICULTY_EASY, "Easy"),
        (DIFFICULTY_MEDIUM, "Medium"),
        (DIFFICULTY_HARD, "Hard"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # NULL = a STANDALONE BANK QUESTION, owned by no quiz.
    #
    # This was non-nullable until the public Quiz Hub work. Every bank
    # question used to be a physical child of exactly one Quiz, so the "bank"
    # was only ever a query-time view (filter on bank_state) and reuse was
    # copy-on-pick — "add from bank" creates a fresh row and edits never
    # propagate. That is fine for a teacher assembling a class test, but a
    # public practice question belongs to no course, no subject and no batch,
    # so under the old shape it had nowhere to live.
    #
    # ⚠ THE FAILURE MODE HERE IS SILENT. Roughly twenty filters across
    # views.py reach classification through the quiz (quiz__subject,
    # quiz__created_by, quiz__chapter_tags). Against a NULL quiz those joins
    # match nothing, so a standalone question does not error — it simply
    # never appears. Every one of those call sites has a test asserting a
    # NULL-quiz question IS returned; do not remove them.
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="questions",
        null=True,
        blank=True,
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

    # Optional classification. Every facet is a QuestionTag row (subject,
    # exam, topic, …) so a new axis costs an INSERT rather than a migration.
    # blank=True is load-bearing: a question must be insertable with NO tags
    # at all and still be servable — bulk-imported rows arrive untagged and
    # get classified afterwards.
    tags = models.ManyToManyField(
        "QuestionTag", blank=True, related_name="questions",
    )

    # The one facet that is a real column rather than a tag, because it is
    # range-queried and sorted ("2019 onwards", "newest first") and a string
    # tag would make that a text comparison.
    year = models.PositiveSmallIntegerField(
        null=True, blank=True, db_index=True,
        help_text="Exam year this question is from, when known.",
    )

    # ⚠ ONLY "single" IS IMPLEMENTED. The column exists now so that adding
    # multi-select or numeric answers later is a widening of the choices
    # rather than a schema rewrite of every consumer — but the serializers
    # reject anything else today, deliberately. Single-select is baked deep:
    # exactly one Choice.is_correct is enforced in the serializers, and
    # StudentAnswer.selected_choice is a single non-null FK that cannot
    # represent two selections or a typed value. Shipping a choice the code
    # cannot honour would be worse than not having the column.
    TYPE_SINGLE = "single"
    TYPE_MULTI = "multi"
    TYPE_NUMERIC = "numeric"
    TYPE_CHOICES = [
        (TYPE_SINGLE, "Single correct answer"),
        (TYPE_MULTI, "Multiple correct answers (not implemented)"),
        (TYPE_NUMERIC, "Numeric answer (not implemented)"),
    ]
    question_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default=TYPE_SINGLE,
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
        # quiz is nullable now — a standalone bank question has no title to
        # borrow, and dereferencing it here would blow up the admin changelist
        # and every error message that interpolates a Question.
        if self.quiz_id is None:
            return f"Bank question: {self.text[:60]}"
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


class PracticeSet(models.Model):
    """A public Quiz Hub practice set (design_handoff_public_quiz_hub Phase 5).

    ⚠ NOT `PracticeSession`, which is above and is a different feature — that
    is one learner's run through a course CHAPTER. This is a course-less,
    batch-less, subject-less-in-the-`courses`-sense set that anybody on the
    public site can practise, including signed-out visitors.

    It deliberately CANNOT be a `Quiz`: `Quiz.subject` is a required FK to
    `courses.Subject`, which is scoped to one course, so every public set
    would have to be filed under some arbitrary course and would inherit that
    course's batch visibility rules.

    ── Why membership is a QUERY, not a stored list ────────────────────────
    The reference design settles this itself. Its own fixture builds a set as
    an AUTHORED description over a GENERATED selection:

        q.questions = rotate(BANK[q.subject], q.seed)

    …while `title`, `desc`, `diff`, `mins` and `exams` are written by hand.
    So this model stores the editorial half and resolves the questions at
    read time:

    * A title like "Ancient India — SSC History Quiz 01" cannot be derived
      from tags, so it has to be stored.
    * Pinning an explicit question list would freeze a set at the moment it
      was made. Curation is ongoing — 3,793 rows arrive `suggested` and are
      accepted over time — so a stored list would be mostly-empty on day one
      and permanently stale afterwards. A query grows as the bank is curated,
      with no further human work.

    The cost of that choice, stated plainly: a set's questions can CHANGE
    between two attempts as curation lands. That is why Phase 6 must record
    the questions it actually served on the attempt itself rather than
    re-deriving them from the set — otherwise a learner's review screen would
    show questions they never saw.
    """

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft — not on the public site"),
        (STATUS_PUBLISHED, "Published — anyone can practise it"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    description = models.TextField(blank=True)

    # The rail this set belongs to. Required: a set with no subject has
    # nowhere to appear on the page.
    subject_tag = models.ForeignKey(
        QuestionTag,
        on_delete=models.PROTECT,
        related_name="practice_sets",
        help_text="A kind='subject' tag. Drives both the card and the query.",
    )
    # Optional narrowing. `exam_tag` doubles as the card's exam chip.
    exam_tag = models.ForeignKey(
        QuestionTag,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="practice_sets_by_exam",
    )
    # "" means "any difficulty" — a real state, not a missing value, so this
    # is blank-able rather than nullable.
    difficulty = models.CharField(
        max_length=10, blank=True, choices=Question.DIFFICULTY_CHOICES)

    question_count = models.PositiveSmallIntegerField(
        default=10, help_text="How many questions to serve, at most.")
    minutes = models.PositiveSmallIntegerField(default=10)
    # Two sets over the same subject show different questions by starting at
    # different offsets — exactly what the design's `seed` does.
    seed = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "title"]
        indexes = [models.Index(fields=["status", "display_order"])]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Derived once and then left alone: re-deriving on rename would break
        # every link and bookmark already pointing at the old slug. Same
        # reasoning as QuestionTag.slug.
        if not self.slug:
            base = slugify(self.title)[:170] or "practice-set"
            slug, n = base, 2
            while PracticeSet.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def question_queryset(self):
        """Every bank question eligible for this set.

        `publishable()` is what keeps a half-curated bank off the public site:
        it demands `accepted` AND an explanation AND exactly one correct
        choice. A `suggested` row can never leak here.

        ⚠ The two `.filter(tags=…)` calls are chained, not combined into a
        single `tags__in`: chained means "has the subject tag AND the exam
        tag", `tags__in` would mean "either". `.distinct()` because each
        join can otherwise repeat a row per matching tag.
        """
        qs = (Question.objects.publishable()
              .filter(quiz__isnull=True, tags=self.subject_tag))
        if self.exam_tag_id:
            qs = qs.filter(tags=self.exam_tag)
        if self.difficulty:
            qs = qs.filter(difficulty=self.difficulty)
        return qs.distinct()

    def pick_questions(self):
        """The questions this set serves, in a STABLE order.

        Stability is the whole point — ordering by `id` is arbitrary but
        repeatable, so the same set serves the same questions in the same
        order on every request. Anything random here would mean a learner
        who reloads mid-attempt gets a different paper.
        """
        ids = list(self.question_queryset().order_by("id")
                   .values_list("id", flat=True))
        if not ids:
            return []
        start = self.seed % len(ids)
        chosen = (ids[start:] + ids[:start])[:self.question_count]
        by_id = {
            q.id: q for q in
            Question.objects.filter(id__in=chosen).prefetch_related("choices")
        }
        return [by_id[i] for i in chosen if i in by_id]

    @property
    def available_count(self):
        """How many questions it can actually serve right now — which is not
        `question_count` when the bank has not been curated that far yet.
        The card must show this, or it advertises 10 questions and serves 3."""
        return min(self.question_count, self.question_queryset().count())


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


class PublicAttempt(models.Model):
    """One run at a PracticeSet from the public Quiz Hub
    (design_handoff_public_quiz_hub Phase 6).

    ── Why this is not QuizAttempt ─────────────────────────────────────────
    `QuizAttempt` carries two conditional unique constraints keyed on
    (quiz, learner_profile|student, attempt_number), so it cannot represent
    "the same anonymous visitor, twice, with no user at all". And its
    `StudentAnswer.selected_choice` is NOT NULL, so it cannot record a
    question left BLANK — which is half of what a review screen has to show.

    ── Anonymous rows are real rows ────────────────────────────────────────
    `account` is nullable and a signed-out attempt is still stored. That is
    what makes an honest `attempt_count` possible on the set card (the design
    wants social proof; Phase 5 deliberately shipped no such field rather
    than fake one). It also means the id below is a CAPABILITY: whoever holds
    the UUID can read that attempt's review. For a signed-in attempt the view
    additionally checks ownership, so a leaked id cannot expose someone's
    account history.

    ⚠ Creating a row is an ANONYMOUS WRITE, so the start endpoint carries a
    ScopedRateThrottle (`quiz_attempt_start`, 100/hour per IP). One call
    writes this row plus one PublicAttemptAnswer per served question, which
    is what made an unthrottled version trivially floodable.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    practice_set = models.ForeignKey(
        "PracticeSet", on_delete=models.PROTECT, related_name="attempts")
    # The ACCOUNT, not a LearnerProfile: the public site is not profile-scoped
    # and a visitor may hold no learner profile at all (a teacher, or someone
    # who only ever reads the marketing site). Requiring one would make the
    # hub unusable for exactly the people the page is advertised to.
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="public_attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    # Frozen at submit. Never recomputed — see PublicAttemptAnswer.is_correct.
    score = models.PositiveSmallIntegerField(default=0)
    total = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["account", "-started_at"]),
            models.Index(fields=["practice_set", "submitted_at"]),
        ]

    def __str__(self):
        return f"PublicAttempt {self.id} · {self.practice_set_id}"

    @property
    def is_submitted(self):
        return self.submitted_at is not None


class PublicAttemptAnswer(models.Model):
    """One question as it was SERVED, plus what was chosen for it.

    ⚠ THIS IS THE SNAPSHOT PHASE 5 REQUIRES. A PracticeSet resolves its
    questions with a query, so its membership changes as curation lands. If
    the review screen re-derived the paper from the set, a learner could
    submit ten questions and be shown a review of a different ten. These rows
    are written when the attempt STARTS, one per question served, and the
    review reads only these.

    Three fields exist purely so a later admin edit cannot rewrite history:

    * `selected_choice` is SET_NULL because the admin bank editor replaces a
      question's choices WHOLESALE (delete-all + bulk_create) on every PATCH
      that includes them. Without SET_NULL that edit would cascade away a
      submitted answer; with it, the FK goes null — which is why…
    * `selected_text` snapshots what the learner actually picked, so the
      review still reads correctly after the options have been rewritten. A
      NULL choice with text tells you "their option no longer exists", which
      is different from…
    * `selected_choice IS NULL AND selected_text = ''`, which means LEFT
      BLANK — the state QuizAttempt structurally cannot store.

    `is_correct` is likewise frozen at submit rather than derived on read: an
    admin correcting an answer key next week must not silently change a score
    somebody already saw.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.ForeignKey(
        PublicAttempt, on_delete=models.CASCADE, related_name="answers")
    # PROTECT, not CASCADE: a bank question with attempts against it must not
    # be deletable out from under them. _bank_question_usage() lists this
    # relation so the admin gets a readable 409 long before PROTECT fires.
    question = models.ForeignKey(
        Question, on_delete=models.PROTECT, related_name="public_attempt_answers")
    order = models.PositiveSmallIntegerField(default=0)
    selected_choice = models.ForeignKey(
        Choice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="public_attempt_answers")
    selected_text = models.CharField(max_length=500, blank=True)
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "question"],
                name="uniq_public_attempt_question"),
        ]

    def __str__(self):
        return f"{self.attempt_id} · q{self.order}"
