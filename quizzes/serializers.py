from .models import Quiz

from datetime import timedelta
from decimal import Decimal
from django.db.models import Avg, Max, Min, Count, Q
import uuid
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied

from courses.board_display import board_name_via
from courses.models import Batch, Chapter
from courses.services import is_teacher_of, resolve_or_create_chapter, teaches_subject
from courses.chapter_tags import ChapterTagWriteMixin, serialize_tags, set_tags
from enrollments.models import Enrollment

from .models import (
    Quiz,
    QuizSection,
    Question,
    Choice,
    QuizAttempt,
    StudentAnswer,
    QuestionTag,
    PracticeSet,
    PublicAttempt,
    PublicAttemptAnswer,
)

# Absorbs real network/render lag on a legitimate last-second auto-submit;
# not meant to give any meaningful extra working time.
SUBMIT_GRACE_SECONDS = 20


def negative_marks_for(quiz):
    """Marks to deduct per WRONG answer on `quiz`, as a Decimal.

    The `quiz_type == "mock"` gate lives here, in one function, rather than
    at the call site: a practice attempt must never subtract, whatever
    `negative_marks_per_wrong` happens to hold (a quiz switched mock →
    practice keeps its configured penalty so switching back doesn't lose it,
    which means a stored non-zero value on a practice quiz is normal, not a
    bug). Returning Decimal("0") rather than short-circuiting keeps the
    scoring loop branch-free and Decimal-only.
    """
    if quiz.quiz_type != Quiz.TYPE_MOCK:
        return Decimal("0")
    # DecimalField gives a Decimal back from the DB, but an in-memory Quiz
    # built with `negative_marks_per_wrong=0.25` (a float literal, as tests
    # and shells do) has not been through the field's to_python — so coerce
    # via str() and never let a float into the arithmetic.
    return Decimal(str(quiz.negative_marks_per_wrong or 0))


class ChoiceAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text", "is_correct"]


class ChoicePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text"]


class QuizSectionSerializer(serializers.ModelSerializer):
    """A mock paper's section. `id` is WRITABLE on input on purpose — it is
    how PUT /teacher/quizzes/:pk/sections/ tells "rename section A" apart
    from "delete A, add B"; see TeacherQuizSectionsView for why that
    distinction is load-bearing."""

    id = serializers.UUIDField(required=False)
    question_count = serializers.SerializerMethodField()

    class Meta:
        model = QuizSection
        fields = ["id", "name", "order", "instructions", "question_count"]

    def get_question_count(self, obj):
        return obj.questions.count()


class QuestionCreateSerializer(serializers.ModelSerializer):
    choices = ChoiceAdminSerializer(many=True)
    # Optional mock-paper grouping. Validated against the quiz in context so
    # a question can't be filed into another teacher's section.
    section = serializers.PrimaryKeyRelatedField(
        queryset=QuizSection.objects.all(), required=False, allow_null=True,
    )

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices",
                  "explanation", "topic", "difficulty", "section"]
        read_only_fields = ["id"]

    def validate_section(self, section):
        quiz = self.context.get("quiz")
        if section is not None and quiz is not None and section.quiz_id != quiz.id:
            raise ValidationError("That section belongs to a different quiz.")
        return section

    def validate(self, attrs):
        choices = attrs.get("choices", [])
        if len(choices) < 2:
            raise ValidationError("At least two choices required.")
        correct_count = sum(1 for c in choices if c.get("is_correct"))
        if correct_count != 1:
            raise ValidationError("Exactly one correct answer required.")
        if not attrs.get("explanation"):
            raise ValidationError("Explanation is required.")
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        choices_data = validated_data.pop("choices")
        quiz = self.context["quiz"]
        question = Question.objects.create(quiz=quiz, **validated_data)
        Choice.objects.bulk_create([
            Choice(question=question, **choice)
            for choice in choices_data
        ])
        return question


class BulkQuestionCreateSerializer(serializers.Serializer):
    """Accepts a list of questions (same shape as QuestionCreateSerializer)
    so the bulk-paste importer and the "add from bank" drawer can create
    many questions on a draft quiz in a single request."""
    questions = QuestionCreateSerializer(many=True)

    @transaction.atomic
    def create(self, validated_data):
        quiz = self.context["quiz"]
        created = []
        start_order = quiz.questions.count()
        for i, q_data in enumerate(validated_data["questions"]):
            choices_data = q_data.pop("choices")
            q_data.setdefault("order", start_order + i)
            question = Question.objects.create(quiz=quiz, **q_data)
            Choice.objects.bulk_create([
                Choice(question=question, **choice) for choice in choices_data
            ])
            created.append(question)
        return created


class QuizCreateSerializer(ChapterTagWriteMixin, serializers.ModelSerializer):
    # Also used (with partial=True) by TeacherUpdateQuizView to edit a draft's
    # title/quiz_type/time_limit_minutes — batch/chapter are create-only
    # (see validate()), matching how Assignment editing leaves batch alone.

    # Cohort-relative, like assignments: a batch is required on create.
    #
    # NO `source="batch"`: Phase 10 dropped the single-batch shim, so this maps
    # onto no model field. It stays in the API because the builder sends it —
    # validate() resolves it and create() writes it into the `batches` M2M,
    # which is the only scope there is.
    batch_id = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(), write_only=True,
    )
    # Existing chapter, or type a new one via custom_chapter — resolved in
    # validate() via resolve_or_create_chapter(), same as assignments/materials.
    #
    # NO `source="chapter"` any more: Phase 10 dropped Quiz.chapter, so this
    # can no longer map onto a model field. It stays in the API because the
    # live QuizBuilder still sends it — validate() resolves it and
    # _apply_tags() writes it as a ContentChapterTag instead.
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(), write_only=True, required=False,
    )
    custom_chapter = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
    )
    # New multi-value payload. Coexists with the two legacy keys above,
    # which the live QuizBuilder still sends.
    chapter_tags = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True,
    )
    save_chapters_to_course = serializers.BooleanField(
        required=False, write_only=True,
    )

    class Meta:
        model = Quiz
        fields = ["id", "subject", "batch_id", "chapter_id", "custom_chapter",
                  "chapter_tags", "save_chapters_to_course",
                  "chapter_note", "no_specific_chapter",
                  "title", "description", "time_limit_minutes", "quiz_type",
                  "reveal_answers_after",
                  # Phase 4 mock-test settings.
                  "negative_marks_per_wrong", "max_attempts",
                  "shuffle_questions", "reveal_answers"]
        read_only_fields = ["id"]

    def validate_subject(self, subject):
        user = self.context["request"].user
        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")
        if not teaches_subject(user, subject):
            raise PermissionDenied("You are not assigned to this subject.")
        return subject

    def validate(self, attrs):
        custom_chapter = attrs.pop("custom_chapter", "")

        # Popped BEFORE the partial-edit shortcut below: chapter_tags and
        # save_chapters_to_course are not model fields, so leaving them in
        # attrs on a PATCH would have ModelSerializer try to setattr them on
        # the Quiz and blow up. Re-tagging an existing quiz is a legitimate
        # edit, so update() applies them.
        self._tag_input = self.pop_chapter_tag_input(attrs)

        if self.instance is not None:
            # A partial edit (TeacherUpdateQuizView) never touches batch or
            # chapter — nothing else to resolve.
            self._tag_subject = self.instance.subject
            return attrs

        subject = attrs.get("subject")
        # Popped: no `batch` column to receive it any more.
        batch = attrs.pop("batch_id", None)
        # Popped, not read: `chapter_id` is no longer a model field (Phase 10
        # dropped Quiz.chapter), so leaving it in attrs would have
        # ModelSerializer try to setattr it on the Quiz.
        chapter = attrs.pop("chapter_id", None)
        user = self.context["request"].user

        if subject and batch:
            if batch.course_id != subject.course_id:
                raise ValidationError(
                    {"batch_id": "Batch and subject belong to different courses."}
                )
            if not is_teacher_of(user, batch, subject):
                raise ValidationError(
                    {"non_field_errors": [
                        f"You are not assigned to teach {subject.name} in "
                        f"{batch.name}. Pick a batch you teach."
                    ]}
                )

        # Chapter is OPTIONAL as of Phase 3 — a question bank or a mixed
        # mock test may map to no single chapter. Authorization does not
        # depend on it (validate_subject + the is_teacher_of check above
        # both run on `subject`), so relaxing this opens nothing.
        if chapter is None:
            if custom_chapter.strip():
                chapter = resolve_or_create_chapter(
                    subject, custom_title=custom_chapter,
                    created_by=self.context["request"].user,
                )
        elif subject and chapter.subject_id != subject.id:
            raise ValidationError(
                {"chapter_id": "Pick a chapter from this quiz's own subject."}
            )

        # Stashed for after the row exists. Neither can ride in attrs — there
        # is no `chapter` and no `batch` column to receive them.
        self._legacy_chapter = chapter
        self._batch = batch
        self._tag_subject = subject
        return attrs

    def _apply_tags(self, quiz):
        tags, save_to_course, present = getattr(
            self, "_tag_input", ([], False, False)
        )
        quiz = self.apply_chapter_tags(
            quiz, getattr(self, "_tag_subject", None) or quiz.subject,
            tags, save_to_course, present,
        )

        # TURN THE LEGACY chapter_id / custom_chapter INPUT INTO A TAG.
        #
        # apply_chapter_tags() returns early when `present` is False, so the
        # pre-Phase-3 path — which the live QuizBuilder and every older client
        # still use — would otherwise record no chapter anywhere at all now
        # that Quiz.chapter is gone. Before Phase 10 it at least landed in the
        # FK, which is how quizzes ended up with a chapter and zero tag rows
        # and made serialize_tags() come back empty on S3.
        #
        # Tags are the only representation left, so the legacy keys write one.
        legacy = getattr(self, "_legacy_chapter", None)
        if not present and legacy is not None:
            set_tags(quiz, [(legacy, "", 0)])
        return quiz

    # Atomic for the same reason as the assignment serializer: _apply_tags
    # resolves the payload after the row exists (tags need the pk) and can
    # still raise there, which otherwise left a committed quiz behind a 400.
    @transaction.atomic
    def create(self, validated_data):
        # ── Per-quiz-type defaults (Phase 4) ──────────────────────────────
        # The spec wants `reveal_answers=after_submit` + `max_attempts=1` for
        # a mock and `after_each` + unlimited for practice. That cannot be a
        # plain field default (one column, two defaults), and it must NOT be
        # in Quiz.save() either: save() runs on every edit, so it would
        # silently re-impose max_attempts=1 on a mock a teacher had
        # deliberately opened up, and would retroactively cap the thousands
        # of existing unlimited mock quizzes the moment anything touched
        # them. Creation time is the only correct hook — a default is a
        # starting value, not an invariant.
        #
        # Only applied when the client omitted the key entirely, so an
        # explicit `max_attempts: null` from the builder still means
        # "unlimited" on a mock.
        # `or TYPE_MOCK`: quiz_type's own model default is mock, so an omitted
        # quiz_type produces a mock and must get the mock defaults too.
        if (validated_data.get("quiz_type") or Quiz.TYPE_MOCK) == Quiz.TYPE_MOCK:
            validated_data.setdefault("reveal_answers", Quiz.REVEAL_AFTER_SUBMIT)
            if "max_attempts" not in validated_data:
                validated_data["max_attempts"] = 1
        quiz = Quiz.objects.create(
            created_by=self.context["request"].user,
            **validated_data
        )

        # SCOPE GOES STRAIGHT INTO THE M2M.
        #
        # This used to write only the pre-Phase-2 `batch` shim, leaving
        # `batches` empty — which quizzes/visibility.py reads as "every batch
        # of the course", and only its fallback to the shim kept that correct.
        # Migration 0021 backfilled the M2M once and every create afterwards
        # reintroduced the divergence. Writing the M2M here is what let the
        # shim, and that fallback, be removed (migration 0032) without a
        # batch-scoped quiz silently widening to the whole course.
        batch = getattr(self, "_batch", None)
        if batch is not None:
            quiz.batches.set([batch])

        return self._apply_tags(quiz)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._apply_tags(super().update(instance, validated_data))


class QuizAssignSerializer(serializers.Serializer):
    """Body for PATCH /teacher/quizzes/:pk/assign/ — the Phase 1 endpoint that
    lets a teacher make their own quiz live for their own batches with no admin
    involvement.

    `batch_ids` is deliberately tri-state:
      · absent      → leave the existing batch scope untouched
      · []          → course-wide (every batch of the course)
      · [a, b, ...] → only those batches

    Treating "absent" as "course-wide" would mean a bare `{"assign": false}`
    silently widened a batch-scoped quiz to the whole course, so the next
    re-assign leaks it to every batch. See TeacherQuizAssignView.
    """

    assign = serializers.BooleanField()
    batch_ids = serializers.ListField(
        child=serializers.PrimaryKeyRelatedField(queryset=Batch.objects.all()),
        required=False,
        allow_empty=True,
    )

    def validate_batch_ids(self, batches):
        # Never trust a batch id from the payload: without this, a teacher
        # could assign their quiz to a batch of a course they have nothing to
        # do with, and every student in it would see it. Same class of bug as
        # the cross-batch LiveKit token leak.
        quiz = self.context["quiz"]
        course_id = quiz.subject.course_id
        stray = sorted(str(b.id) for b in batches if b.course_id != course_id)
        if stray:
            raise ValidationError(
                "These batches belong to a different course than this quiz's "
                f"subject: {', '.join(stray)}."
            )
        # De-dupe while preserving order, so batch_ids[0] (the legacy FK shim)
        # is predictable.
        seen, unique = set(), []
        for b in batches:
            if b.id not in seen:
                seen.add(b.id)
                unique.append(b)
        return unique


class QuizDashboardSerializer(serializers.ModelSerializer):
    subject_id = serializers.UUIDField(read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    board_name = serializers.SerializerMethodField()
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True, default=None)
    questions_count = serializers.IntegerField(read_only=True)
    status = serializers.SerializerMethodField()
    score = serializers.SerializerMethodField()
    attempts_count = serializers.SerializerMethodField()
    best_score = serializers.SerializerMethodField()
    last_attempt_at = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "subject_id", "subject_name", "course_title",
            "board_name", "teacher_name",
            "created_at", "total_marks", "questions_count", "time_limit_minutes",
            # chapter_note is the teacher's free-text note for the quiz
            # (quizzes/0024). S1 renders it as the quoted line under each
            # assigned row (README §S1); without it here the quote silently
            # never appears, because a missing key reads as "no note".
            "status", "score", "best_score", "last_attempt_at", "chapter_note",
            "attempts_count", "quiz_type",
        ]

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_status(self, obj):
        # Primary: use prefetched data
        attempts = getattr(obj, "user_submitted_attempts", [])
        if attempts:
            return "SUBMITTED"
        # Fallback: use submitted_ids set passed via context
        submitted_ids = self.context.get("submitted_ids", set())
        if obj.id in submitted_ids:
            return "SUBMITTED"
        pending = getattr(obj, "user_attempts", [])
        if pending:
            return "PENDING"
        return "NOT_STARTED"

    def get_score(self, obj):
        attempts = getattr(obj, "user_submitted_attempts", [])
        if not attempts:
            return None
        return attempts[0].score

    def get_last_attempt_at(self, obj):
        # When the learner last FINISHED this quiz, for S1's "Your last
        # attempts" rail (design_handoff_quiz_system README §S1), which pairs
        # each score tile with a "when". `score`/`best_score` above already
        # read this same prefetch, so this adds no query.
        #
        # The prefetch is ordered by `-attempt_number`, not by time, so [0] is
        # the highest-numbered attempt rather than the newest-submitted one.
        # Those are normally the same row, but an older attempt auto-submitted
        # late by the expiry sweep can carry a newer `submitted_at` — so take
        # the max rather than trusting the ordering.
        attempts = getattr(obj, "user_submitted_attempts", [])
        stamps = [a.submitted_at for a in attempts if a.submitted_at]
        return max(stamps) if stamps else None

    def get_attempts_count(self, obj):
        # Primary: prefetched
        attempts = getattr(obj, "user_submitted_attempts", [])
        if attempts:
            return len(attempts)
        # Fallback: at least 1 if in submitted_ids
        submitted_ids = self.context.get("submitted_ids", set())
        return 1 if obj.id in submitted_ids else 0

    def get_best_score(self, obj):
        # Best-ever percent across every submitted attempt (not just the
        # latest, which `score` above reflects) — reuses the same prefetch,
        # no extra query.
        attempts = getattr(obj, "user_submitted_attempts", [])
        if not attempts or not obj.total_marks:
            return None
        best = max(a.score for a in attempts)
        # Clamped: a quiz whose total_marks fell out of sync with its
        # questions (edited after an attempt was scored) can otherwise
        # divide out to well over 100% — seen live on a dev-seeded quiz.
        return round(min(100.0, best * 100.0 / obj.total_marks), 1)


class QuizSubmitSerializer(serializers.Serializer):
    answers = serializers.ListField(
        child=serializers.DictField(), allow_empty=True)

    def validate(self, attrs):
        quiz = self.context["quiz"]
        user = self.context["request"].user

        from enrollments.services import has_active_subscription
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(self.context["request"])
        if not has_active_subscription(user=user, course=quiz.subject.course, learner_profile=learner):
            raise ValidationError("Your subscription for this course has expired.")

        # Defence-in-depth mirror of SubmitQuizView's own `is_assigned` gate.
        # Reads is_assigned, NOT is_published: after Phase 1 a teacher-assigned
        # quiz is live without ever being admin-approved, and this check ran on
        # every submit — left on is_published it would reject every legitimate
        # submission to a quiz the teacher assigned themselves.
        if not quiz.is_assigned:
            raise ValidationError("Quiz not assigned.")

        # Allow partial submission (auto-submit on timer expiry)
        # We do NOT validate all questions answered here — partial is OK.
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        from accounts.auth_flow import get_active_profile

        quiz = self.context["quiz"]
        user = self.context["request"].user
        submitted_answers = self.validated_data["answers"]

        # Finalize the ACTIVE PROFILE's attempt — never a sibling's.
        learner = get_active_profile(self.context["request"])
        if learner is None:
            raise ValidationError("Select a learner profile first.")

        attempt = QuizAttempt.objects.select_for_update().filter(
            quiz=quiz,
            learner_profile=learner,
            status=QuizAttempt.STATUS_PENDING,
        ).order_by("-attempt_number").first()

        if not attempt:
            raise ValidationError(
                "No active attempt found. Please start the quiz first.")

        # time_limit_minutes was previously enforced only by the frontend's
        # localStorage-backed countdown, which a student can reset by
        # clearing that key — the deadline itself was never checked here.
        # A small grace period absorbs real network/render lag on a
        # legitimate last-second auto-submit; StartQuizView is the one that
        # actually closes out an attempt that missed even the grace period,
        # so a stale PENDING attempt never blocks a fresh one.
        if quiz.time_limit_minutes:
            deadline = attempt.started_at + timedelta(
                minutes=quiz.time_limit_minutes, seconds=SUBMIT_GRACE_SECONDS,
            )
            if timezone.now() > deadline:
                raise ValidationError(
                    "Time's up for this attempt — it has been closed out. "
                    "Start a new attempt to try again."
                )

        # ── Scoring ───────────────────────────────────────────────────────
        # Decimal, not float, for the whole computation: the UI offers 0.33 as
        # a penalty, and 3 × 0.33 in binary floating point is
        # 0.9899999999999999, so a float score fails an exact comparison and
        # renders as 9.010000000000002 on the result screen. Decimal("0.33")
        # is exactly 0.33. `question.marks` is an int, which Decimal absorbs
        # exactly — nothing here may introduce a float.
        #
        # `attempt.score` is a FloatField (pre-existing, and the denominator
        # of several Avg()/Max() aggregates elsewhere), so the final value is
        # quantized to 2dp and converted ONCE, at the boundary. Every value
        # this can produce is exactly representable to 2dp, so the stored
        # double round-trips.
        penalty = negative_marks_for(quiz)
        score = Decimal("0")
        attempt.answers.all().delete()

        for item in submitted_answers:
            question_id = item.get("question")
            choice_id = item.get("selected_choice")

            question = Question.objects.filter(
                id=question_id, quiz=quiz).first()
            if not question:
                continue  # skip invalid questions gracefully

            choice = Choice.objects.filter(
                id=choice_id, question=question).first()
            if not choice:
                # BLANK / unanswered — and this is the ONLY path a blank can
                # take, in either of its two spellings: the question is
                # missing from `answers` entirely, or it is present with
                # `selected_choice: null` (which is what the mock screen
                # sends for a question the learner visited but skipped).
                # Both land here, both are skipped, and neither is penalised.
                # StudentAnswer.selected_choice is a non-nullable FK, so a
                # persisted "answered with nothing" row cannot exist — a
                # blank is always the absence of a row.
                #
                # Not penalising blanks is not a nicety: every Indian
                # competitive exam this platform serves marks only attempted
                # questions, and the results screen counts "Blank" separately
                # from "Got wrong".
                continue

            if choice.is_correct:
                score += Decimal(question.marks)
            else:
                score -= penalty

            time_spent = item.get("time_spent") or item.get("time_spent_seconds") or 0
            try:
                time_spent = max(0, int(time_spent))
            except (TypeError, ValueError):
                time_spent = 0

            StudentAnswer.objects.create(
                attempt=attempt,
                question=question,
                selected_choice=choice,
                is_correct=choice.is_correct,
                time_spent_seconds=time_spent,
                marked_for_review=bool(item.get("marked_for_review", False)),
            )

        # NOT floored at zero. A mock with negative marking can legitimately
        # score below 0, which is how the real exams work and is information
        # the learner needs (it says "you guessed too much", where a clamped
        # 0 says "you knew nothing"). Flagged in the handoff as a product
        # decision to confirm; if it must be floored, do it HERE and nowhere
        # else.
        attempt.score = float(score.quantize(Decimal("0.01")))
        attempt.status = QuizAttempt.STATUS_SUBMITTED
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["score", "status", "submitted_at"])
        return attempt


class QuestionPublicSerializer(serializers.ModelSerializer):
    choices = ChoicePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices", "topic",
                  "difficulty", "section"]
        # NOTE: explanation is intentionally omitted from the public serializer
        # so students don't see it before submitting


class QuestionTeacherSerializer(serializers.ModelSerializer):
    """Full question data including correct answers — for teacher draft preview."""
    choices = ChoiceAdminSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        # `suggest_to_bank` is read back so the builder's per-question switch
        # can show its real state. Without it the builder would default every
        # switch to on and send that back on the next save, silently
        # re-suggesting questions the teacher had deliberately kept private —
        # the same read-gap-becomes-destructive-write shape as the missing
        # chapter fields on QuizDetailTeacherSerializer. `bank_state` rides
        # along read-only so the UI can tell "an admin already accepted this"
        # apart from "still queued".
        fields = ["id", "text", "marks", "order", "choices",
                  "explanation", "topic", "difficulty", "section",
                  "suggest_to_bank", "bank_state"]
        read_only_fields = ["bank_state"]


class QuizDetailSerializer(serializers.ModelSerializer):
    """Student-facing quiz detail — no correct answers exposed."""
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    board_name = serializers.SerializerMethodField()
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True, default=None)
    questions = serializers.SerializerMethodField()
    sections = QuizSectionSerializer(many=True, read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_name", "course_title",
            "board_name",
            "teacher_name", "created_at", "time_limit_minutes", "questions",
            "quiz_type",
            # Phase 4: the attempt screen needs the rules it is enforcing —
            # the penalty to show in the instructions, whether to shuffle,
            # and when answers appear. `sections` is [] for a flat quiz.
            "negative_marks_per_wrong", "max_attempts", "shuffle_questions",
            "reveal_answers", "sections",
        ]

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionPublicSerializer(questions, many=True).data


class QuizDetailTeacherSerializer(serializers.ModelSerializer):
    """
    Teacher draft preview — includes correct answers and explanations.
    Used by QuizDetailDraftView for unpublished quiz review.
    """
    subject_id = serializers.UUIDField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    board_name = serializers.SerializerMethodField()
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True, default=None)
    questions = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    # Named to match QuizAssignSerializer's write field, so the teacher app can
    # round-trip the assign form without renaming anything.
    batch_ids = serializers.PrimaryKeyRelatedField(
        source="batches", many=True, read_only=True,
    )
    sections = QuizSectionSerializer(many=True, read_only=True)
    # The builder's chapter picker reads these to repopulate itself on edit.
    # Without them the picker loads empty and the next save writes an empty
    # `chapter_tags`, silently dropping every chapter the quiz was filed under
    # — a read gap that turns into data loss the moment the write side exists.
    chapter_tags = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_id", "subject_name",
            "course_title", "board_name", "teacher_name", "created_at",
            "time_limit_minutes",
            "questions", "quiz_type", "review_status",
            "review_note", "reviewed_at", "submitted_for_review_at",
            "is_editable", "is_assigned", "batch_ids",
            # Phase 4 mock-test settings, so the builder round-trips them.
            "negative_marks_per_wrong", "max_attempts", "shuffle_questions",
            "reveal_answers", "reveal_answers_after", "sections",
            # Phase 3 chapter tagging, same reason.
            "chapter_tags", "no_specific_chapter", "chapter_note",
        ]

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionTeacherSerializer(questions, many=True).data


class QuestionResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    text = serializers.CharField()
    # allow_blank: a Choice may legitimately have empty text (the teacher's
    # builder does not require it). Without this, one such choice anywhere in
    # an attempt makes is_valid(raise_exception=True) reject the WHOLE result
    # payload, and the student sees "Unable to load result." rather than a
    # blank option. Same reasoning as correct_choice below, which already
    # had it — this field was simply missed.
    selected_choice = serializers.CharField(allow_blank=True, default="")
    correct_choice = serializers.CharField(allow_blank=True, default="")
    is_correct = serializers.BooleanField()
    explanation = serializers.CharField(
        allow_blank=True, default="No explanation")
    topic = serializers.CharField(allow_blank=True, default="")
    difficulty = serializers.CharField(allow_blank=True, default="medium")
    time_spent_seconds = serializers.IntegerField(default=0)
    marked_for_review = serializers.BooleanField(default=False)


class TopicBreakdownSerializer(serializers.Serializer):
    topic = serializers.CharField()
    correct = serializers.IntegerField()
    total = serializers.IntegerField()
    pct = serializers.FloatField()


class DifficultyBreakdownSerializer(serializers.Serializer):
    difficulty = serializers.CharField()
    correct = serializers.IntegerField()
    total = serializers.IntegerField()
    pct = serializers.FloatField()


class ScoreTrendPointSerializer(serializers.Serializer):
    quiz_id = serializers.UUIDField()
    quiz_title = serializers.CharField()
    submitted_at = serializers.DateTimeField()
    pct = serializers.FloatField()
    class_avg_pct = serializers.FloatField()


class QuizResultSerializer(serializers.Serializer):
    quiz_id = serializers.UUIDField()
    title = serializers.CharField()
    subject_name = serializers.CharField()
    course_title = serializers.CharField(allow_blank=True, default="")
    board_name = serializers.CharField(
        allow_blank=True, allow_null=True, required=False, default=None)
    # allow_blank: Quiz.created_by is null=True, and QuizResultView feeds ""
    # for a creatorless quiz (bank/seeded/imported sets). Without this the
    # entire result screen 400s for those quizzes — a whole-page failure
    # caused by one absent byline. course_title and board_name beside it
    # already allowed blank; this one was missed.
    teacher_name = serializers.CharField(allow_blank=True, default="")
    quiz_type = serializers.CharField(default="mock")
    total_marks = serializers.IntegerField()
    # FloatField, NOT IntegerField: with negative marking a score is
    # fractional (and can be negative). IntegerField would have silently
    # truncated 9.01 → 9 and −0.5 → 0 on the way out, i.e. the result screen
    # would have shown a different score than the one stored.
    score = serializers.FloatField()
    submitted_at = serializers.DateTimeField()
    attempt_number = serializers.IntegerField(default=1)
    answers_revealed = serializers.BooleanField(default=True)
    # The paper's full question count. `questions` below is answered-only, so
    # this is the only honest denominator for "N of M correct" / accuracy.
    questions_total = serializers.IntegerField(default=0)
    questions = QuestionResultSerializer(many=True)

    # ── analytics (results + analytics screen) ──────────────────────────
    class_avg_percent = serializers.FloatField(default=0)
    # % of all submitted attempts that scored strictly HIGHER than this one
    # (0 == top score). Kept for back-compat / analytics.
    percentile = serializers.FloatField(default=0)
    # % of all submitted attempts this one scored strictly HIGHER than — the
    # student-friendly, unambiguous framing rendered on the result screen.
    scored_higher_than = serializers.FloatField(default=0)
    topic_breakdown = TopicBreakdownSerializer(many=True, default=list)
    difficulty_breakdown = DifficultyBreakdownSerializer(many=True, default=list)
    score_trend = ScoreTrendPointSerializer(many=True, default=list)
    wrong_question_ids = serializers.ListField(
        child=serializers.UUIDField(), default=list)

    # ── S3 · results screen (Phase 9) ───────────────────────────────────
    # The quiz's chapter tags, each with is_custom so a teacher-created
    # chapter can be marked and offered as chapter practice. QUIZ-level, not
    # per-question — see the note at the call site in QuizResultView.
    chapters = serializers.ListField(child=serializers.DictField(), default=list)
    # Questions on the paper that were never answered. `questions` is
    # answered-only, so this cannot be derived from it on the client.
    blank_count = serializers.IntegerField(default=0)
    marked_count = serializers.IntegerField(default=0)
    # Wall-clock spent on the attempt, for "finished N minutes early".
    time_taken_seconds = serializers.IntegerField(allow_null=True, default=None)
    time_limit_minutes = serializers.IntegerField(allow_null=True, default=None)
    # The previous submitted attempt's percent, or null on a first attempt —
    # null must render as NO verdict, not as a 0-point improvement.
    previous_percent = serializers.FloatField(allow_null=True, default=None)


class TeacherQuizAttemptSerializer(serializers.ModelSerializer):
    student_id = serializers.UUIDField(source="student.id", read_only=True)
    student_email = serializers.EmailField(
        source="student.email", read_only=True)
    student_name = serializers.SerializerMethodField()
    learner_profile_id = serializers.UUIDField(
        source="learner_profile.id", read_only=True, default=None)
    total_marks = serializers.IntegerField(
        source="quiz.total_marks", read_only=True)

    class Meta:
        model = QuizAttempt
        fields = [
            "id", "student_id", "student_email", "student_name",
            "learner_profile_id",
            "score", "total_marks", "submitted_at", "attempt_number",
        ]

    def get_student_name(self, obj):
        # The learner who actually took it — on a shared family account the
        # account username can't distinguish between children.
        lp = obj.learner_profile or obj.student.default_learner_profile()
        if lp:
            name = (lp.full_name or "").strip() or (lp.display_name or "").strip()
            if name:
                return name
        return obj.student.username or obj.student.email


class TeacherQuizAnalyticsSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    # The flat faculty Quizzes list needs the id, not just the name, to build
    # subject pills and to target per-subject actions at the right class.
    subject_id = serializers.UUIDField(source="subject.id", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    board_name = serializers.SerializerMethodField()
    questions_count = serializers.IntegerField(read_only=True)
    total_attempts = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    highest_score = serializers.FloatField(read_only=True)
    lowest_score = serializers.FloatField(read_only=True)
    submission_rate = serializers.FloatField(read_only=True)
    # Annotated by the list views; declared so DRF doesn't try to resolve them
    # as model fields when a caller serializes an un-annotated Quiz.
    batch_count = serializers.IntegerField(read_only=True, default=0)
    bank_accepted = serializers.IntegerField(read_only=True, default=0)
    bank_suggested = serializers.IntegerField(read_only=True, default=0)
    bank_changes_requested = serializers.IntegerField(read_only=True, default=0)
    bank_private = serializers.IntegerField(read_only=True, default=0)
    chapter_tags = serializers.SerializerMethodField()

    def get_chapter_tags(self, obj):
        # Reads the `_prefetched_chapter_tags` attribute attach_chapter_tags()
        # sets, so this is free per row rather than a query each.
        return serialize_tags(obj)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "created_at", "subject_name", "subject_id",
            "course_title", "board_name",
            "is_assigned", "questions_count",
            # total_marks is the DENOMINATOR for average/highest/lowest, which
            # are raw marks, not percentages. Without it the Quizzes card had
            # no way to turn "7.5" into "75%" and rendered the mark itself
            # through a percent formatter — a 10-mark quiz with a healthy 7.5
            # class average displayed as "avg 8%".
            "total_marks",
            "total_attempts", "submission_rate", "average_score",
            "highest_score", "lowest_score", "quiz_type", "review_status",
            "review_note",
            # ── T1 row data (Phase 6) ────────────────────────────────────
            # The row's meta line lists the quiz's chapter tags and its
            # timing rules, and it carries two status chips: one for the
            # assignment state (needs the batch count) and one for the
            # site-bank state (needs the per-quiz bank breakdown). All of it
            # comes off annotations/prefetch so a 40-quiz list stays flat
            # rather than firing a query per row.
            "chapter_tags", "no_specific_chapter",
            "batch_count", "time_limit_minutes", "max_attempts",
            "bank_accepted", "bank_suggested", "bank_changes_requested",
            "bank_private",
        ]

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")


class TeacherQuizStudentSummarySerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    student_name = serializers.CharField()
    student_email = serializers.EmailField()
    latest_submitted_at = serializers.DateTimeField()
    best_score = serializers.FloatField()
    average_score = serializers.FloatField()
    total_marks = serializers.IntegerField()
    attempts_count = serializers.IntegerField()


# =====================================================
# QUESTION BANK (teacher: "mine" / "school" reusable questions)
# =====================================================

class BankQuestionSerializer(serializers.ModelSerializer):
    """A question surfaced in the teacher question bank.

    Phase 2: `scope=mine` is every question on the requesting teacher's own
    quizzes regardless of review/curation state (ownership, not admin
    approval, is what makes it "yours"); `scope=school` is still gated on
    `bank_state="accepted"` — the shared ShikshaCom bank other teachers draw
    on. See TeacherQuestionBankView.get_queryset.

    `bank_state`/`suggest_to_bank`/`bank_feedback` are additive fields for
    the T3 "My question bank" screen (state chip + admin-feedback block).
    Do not rename or remove any of the pre-existing fields below — the
    current teacher QuizBank.jsx screen consumes this response shape as-is.
    """
    choices = ChoiceAdminSerializer(many=True, read_only=True)
    # ⚠ EVERY ONE OF THESE NEEDS default=None. Question.quiz is nullable as of
    # the public Quiz Hub work, and a standalone bank question has no quiz to
    # borrow a title, subject or author from. DRF resolves a dotted source by
    # walking the chain with getattr, so `quiz.subject.name` against a NULL
    # quiz raises AttributeError; the default is what turns that into a null
    # in the payload instead of a 500 on the bank list.
    quiz_id = serializers.UUIDField(source="quiz.id", read_only=True, default=None)
    quiz_title = serializers.CharField(source="quiz.title", read_only=True, default=None)
    subject_id = serializers.UUIDField(source="quiz.subject.id", read_only=True, default=None)
    subject_name = serializers.CharField(
        source="quiz.subject.name", read_only=True, default=None)
    author_name = serializers.CharField(
        source="quiz.created_by.email", read_only=True, default=None)
    author_id = serializers.UUIDField(source="quiz.created_by.id", read_only=True, default=None)
    # The classification a standalone question carries in its own right,
    # rather than inheriting from a quiz it does not have.
    tags = serializers.SerializerMethodField()
    # T3's chapter chip. A Question has no chapter of its own — Phase 3 put
    # chapter tagging on the quiz — so this is the quiz's first tag, which is
    # what the question is actually filed under. `chapter_is_custom` drives the
    # spec's warning tint for a teacher-typed chapter that no admin has
    # promoted into the syllabus yet.
    chapter_label = serializers.SerializerMethodField()
    chapter_is_custom = serializers.SerializerMethodField()

    def get_tags(self, obj):
        return [
            {"id": str(t.id), "kind": t.kind, "label": t.label, "slug": t.slug}
            for t in obj.tags.all()
        ]

    def _first_tag(self, obj):
        # A standalone bank question has no quiz, so no chapter tag either —
        # chapter classification lives on the Quiz, not the Question. Bail
        # before touching serialize_tags(None), which would raise.
        if obj.quiz_id is None:
            return None
        # Prefer the map the list view builds (one query for the whole page).
        # select_related gives every Question its OWN Quiz instance, so
        # attach_chapter_tags() on a deduped list would not reach them —
        # hence a plain id→tags dict rather than a prefetch attribute.
        by_quiz = self.context.get("chapter_tags_by_quiz")
        if by_quiz is not None:
            tags = by_quiz.get(obj.quiz_id) or []
        else:
            # Fallback for single-object use; one query, not N.
            tags = serialize_tags(obj.quiz)
        return tags[0] if tags else None

    def get_chapter_label(self, obj):
        tag = self._first_tag(obj)
        return (tag or {}).get("label") or None

    def get_chapter_is_custom(self, obj):
        tag = self._first_tag(obj)
        return bool((tag or {}).get("is_custom"))

    class Meta:
        model = Question
        fields = [
            "id", "text", "marks", "explanation", "topic", "difficulty",
            "choices", "quiz_id", "quiz_title", "subject_id", "subject_name",
            "author_name", "author_id", "created_at", "source",
            # Phase 2 additions — purely additive, see class docstring.
            "bank_state", "suggest_to_bank", "bank_feedback",
            # Phase 6 (T3) additions.
            "chapter_label", "chapter_is_custom",
            # Public Quiz Hub additions — a standalone bank question carries
            # its own classification rather than inheriting a quiz's.
            "tags", "year", "question_type",
        ]


class QuestionBankStateSerializer(serializers.Serializer):
    """Body for PATCH /teacher/questions/:pk/bank/ — the teacher's per-
    question opt-in/out of the shared ShikshaCom bank.

    Only `suggest_to_bank` is teacher-writable. `bank_state` follows from it
    via Question.save()'s invariant (see that method's docstring for why
    turning this off always wins, but turning it on never clobbers an
    admin's existing accept/request-changes decision).
    """
    suggest_to_bank = serializers.BooleanField()


# =====================================================
# ADMIN — standalone bank questions (design_handoff_public_quiz_hub)
# =====================================================

class AdminBankQuestionWriteSerializer(serializers.ModelSerializer):
    """Create/edit a STANDALONE bank question (quiz=None) from the new admin
    authoring screens. Read responses use BankQuestionSerializer instead —
    this one is write-only shape, matching the "write serializer in, read
    serializer out" convention this file already uses for question review
    (see _apply_bank_review's call sites in views.py).

    Two deliberate divergences from the older QuestionCreateSerializer
    (teacher builder, requires a `quiz` in context):

      * `explanation` is OPTIONAL here. QuestionCreateSerializer's own
        validate() hard-requires it; this serializer has no such check, and
        relies on Question.explanation's model-level `blank=True` to make it
        genuinely optional end to end. Previous-year imports legitimately
        arrive with a stem and an answer key but no written explanation, and
        must still be storable — see Question.objects.publishable(), which
        is what actually keeps an unexplained row off the public hub. This
        is the intended split: "in the bank" and "servable to a learner" are
        different questions with different answers.
      * `question_type` is validated to reject anything but "single" — see
        validate_question_type(). The column exists for future multi/numeric
        widening (Question.question_type's own comment in models.py) but
        every consumer downstream (StudentAnswer.selected_choice, the
        exactly-one-correct-choice rule below) only knows how to handle
        single-select today, so accepting the other two values would create
        a question nothing could ever grade.
    """
    choices = ChoiceAdminSerializer(many=True)
    tag_ids = serializers.PrimaryKeyRelatedField(
        source="tags", queryset=QuestionTag.objects.all(),
        many=True, required=False,
    )

    class Meta:
        model = Question
        fields = [
            "id", "text", "explanation", "difficulty", "year", "topic",
            "question_type", "tag_ids", "choices", "bank_state", "created_at",
        ]
        read_only_fields = ["id", "bank_state", "created_at"]

    def validate_question_type(self, value):
        if value != Question.TYPE_SINGLE:
            raise ValidationError(
                'Only "single" is implemented today — see '
                "Question.question_type's comment in models.py. Multi/"
                "numeric answers have no grading path yet."
            )
        return value

    def validate(self, attrs):
        # `choices` is present in `attrs` only when the caller actually sent
        # it. On create the spec requires it in the body, so this always
        # runs there; on a PATCH that only touches e.g. `difficulty`, the
        # existing choices must be left alone rather than forced to be
        # resent.
        choices = attrs.get("choices")
        if choices is None and self.instance is None:
            raise ValidationError({"choices": "At least two choices required."})
        if choices is not None:
            if len(choices) < 2:
                raise ValidationError({"choices": "At least two choices required."})
            correct_count = sum(1 for c in choices if c.get("is_correct"))
            if correct_count != 1:
                raise ValidationError(
                    {"choices": "Exactly one correct answer required."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        choices_data = validated_data.pop("choices")
        tags = validated_data.pop("tags", [])
        # Any `bank_state` the caller sent is ignored on create (it isn't in
        # `validated_data` at all — see Meta.read_only_fields). An admin
        # authoring or importing a question here IS the review; there is no
        # teacher on the other end for it to be "suggested" to, and the
        # existing review queue (AdminQuestionBankQueueView) explicitly
        # excludes quiz__isnull rows, so a standalone question left at the
        # model's default bank_state="suggested" would sit in a queue that
        # can never show it to anybody. "Accepted" here is necessary but not
        # sufficient for the question to reach the public hub — it still
        # needs an explanation and valid choices, which is exactly what
        # Question.objects.publishable() checks independently.
        admin = self.context["request"].user
        question = Question.objects.create(
            quiz=None,
            bank_state=Question.BANK_STATE_ACCEPTED,
            bank_reviewed_by=admin,
            bank_reviewed_at=timezone.now(),
            **validated_data,
        )
        if tags:
            question.tags.set(tags)
        Choice.objects.bulk_create([
            Choice(question=question, **choice) for choice in choices_data
        ])
        return question

    @transaction.atomic
    def update(self, instance, validated_data):
        choices_data = validated_data.pop("choices", None)
        tags = validated_data.pop("tags", None)
        # setattr + save(), never queryset.update() — Question.save() runs
        # the suggest_to_bank/bank_state invariant (models.py) on every
        # write, and .update() would bypass it entirely.
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if tags is not None:
            instance.tags.set(tags)
        if choices_data is not None:
            # Full replace, not a per-choice merge — there is no sane
            # partial-choice semantics ("choice A becomes choice B"? kept
            # in place but re-ordered? still correct?), so like
            # TeacherQuizSectionsView's section replace, the client always
            # sends the complete list and this treats it as authoritative.
            instance.choices.all().delete()
            Choice.objects.bulk_create([
                Choice(question=instance, **choice) for choice in choices_data
            ])
        return instance


def _bank_question_usage(question):
    """What real learner activity references `question`, keyed by kind ->
    count. Empty dict means "safe to hard-delete".

    Mirrors the Content Studio media-delete precedent (409 + `used_in[]`
    rather than a silent cascade or a bare 400) — see
    content/studio_views.py's media delete view. A DELETE here is refused,
    not archived: there is no "archived" bank_state value and adding one
    would be a migration, which this task is explicitly not allowed to make.
    Refusing with the usage breakdown at least tells the admin why, and
    editing (PATCH) or simply leaving the question in place both remain
    available.
    """
    usage = {}
    student_answers = StudentAnswer.objects.filter(question=question).count()
    if student_answers:
        usage["student_answers"] = student_answers
    practice_answers = question.practice_answers.count()
    if practice_answers:
        usage["practice_answers"] = practice_answers
    # M2M related_name from PracticeSession.questions (models.py) — a
    # session that served this question as one of its set, whether or not
    # the learner ever answered it.
    practice_sessions = question.practice_sessions.count()
    if practice_sessions:
        usage["practice_sessions"] = practice_sessions
    # Phase 6. Without this the admin sees "safe to delete", the DELETE then
    # hits PublicAttemptAnswer.question's PROTECT, and a friendly 409 becomes
    # a 500. The point of the guard is to answer BEFORE the database does.
    public_attempt_answers = question.public_attempt_answers.count()
    if public_attempt_answers:
        usage["public_attempt_answers"] = public_attempt_answers
    return usage


# =====================================================
# ADMIN — tag / rail taxonomy (design_handoff_public_quiz_hub)
# =====================================================

def _tags_with_counts(queryset):
    """Annotate a QuestionTag queryset with `question_count` — the number of
    PUBLISHABLE questions carrying each tag — in ONE query for the whole
    page rather than one query per row.

    `Question.objects.publishable()` already does its own aggregation
    (choice counts) to decide what counts as publishable; reusing it as a
    `.values("id")` subquery for the `IN` filter, rather than re-deriving
    "is this question publishable" here in different words, is what keeps
    the two definitions from drifting apart the next time publishable() changes.
    """
    publishable_ids = Question.objects.publishable().values("id")
    return queryset.annotate(
        question_count=Count(
            "questions",
            filter=Q(questions__in=publishable_ids),
            distinct=True,
        )
    )


class AdminQuestionTagSerializer(serializers.ModelSerializer):
    """A subject/exam/topic/custom tag, from the admin taxonomy screens.

    Surfaces BOTH the admin's stored `status` and the server-computed
    `effective_status`, plus `status_downgraded` — never silently resolving
    a disagreement between them. See QuestionTag.effective_status()'s own
    docstring: `live` is a floor an admin can fail to reach, never an
    override they can force with nothing behind it.
    """
    question_count = serializers.SerializerMethodField()
    effective_status = serializers.SerializerMethodField()
    status_downgraded = serializers.SerializerMethodField()

    class Meta:
        model = QuestionTag
        fields = [
            "id", "kind", "label", "slug", "content_tag", "course",
            "status", "effective_status", "status_downgraded",
            "display_order", "icon", "color", "cover_image",
            "question_count", "created_at",
        ]
        read_only_fields = ["id", "slug", "created_at"]

    def _count(self, obj):
        # Prefer the annotation _tags_with_counts() attaches (one query for
        # the whole list/detail queryset). A serializer instantiated around
        # a plain `QuestionTag.objects.create(...)` result right after POST
        # carries no such annotation — fall back to a direct count, which
        # only costs one extra query on that single-object response, never
        # on a list.
        cached = getattr(obj, "question_count", None)
        if cached is not None:
            return cached
        return Question.objects.publishable().filter(tags=obj).count()

    def get_question_count(self, obj):
        return self._count(obj)

    def get_effective_status(self, obj):
        return obj.effective_status(self._count(obj))

    def get_status_downgraded(self, obj):
        # Only "live" can ever disagree with the computed answer — "soon"
        # and "hidden" are both honoured unconditionally by
        # effective_status(), so there is nothing to warn about there.
        return (
            obj.status == QuestionTag.STATUS_LIVE
            and self.get_effective_status(obj) != QuestionTag.STATUS_LIVE
        )

    def validate(self, attrs):
        # ContentTag/CourseCategory precedent (CLAUDE.md Content Studio note
        # 18, and content/studio_views.py's label-create path): a slug
        # collision on save() would raise IntegrityError (UniqueConstraint
        # on kind+slug) rather than a readable 400. Check for the collision
        # ourselves, before it reaches the DB, the same way the Labels
        # screen's create endpoint already does for ContentTag.
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        label = attrs.get("label", getattr(self.instance, "label", None))
        if kind and label:
            from django.utils.text import slugify
            slug = slugify(label)
            existing = QuestionTag.objects.filter(kind=kind, slug=slug)
            if self.instance is not None:
                existing = existing.exclude(pk=self.instance.pk)
            existing = existing.first()
            if existing is not None:
                raise ValidationError({
                    "label": (
                        f"A {existing.get_kind_display().lower()} tag named "
                        f"“{existing.label}” already exists."
                    ),
                })
        return attrs


# =====================================================
# ADMIN — academy quiz verification
# =====================================================

class AdminQuizListSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True, default=None)
    questions_count = serializers.IntegerField(read_only=True)
    attempts_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "subject_name", "course_title", "teacher_name",
            "quiz_type", "review_status", "is_assigned",
            "questions_count",
            "attempts_count", "total_marks", "created_at",
            "submitted_for_review_at", "reviewed_at",
        ]


class AdminQuizDetailSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True, default=None)
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.email", read_only=True, default=None)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_name", "course_title",
            "teacher_name", "quiz_type", "review_status", "review_note",
            "is_assigned", "total_marks", "time_limit_minutes",
            "created_at", "submitted_for_review_at", "reviewed_at",
            "reviewed_by_name", "questions",
        ]

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionTeacherSerializer(questions, many=True).data


class AdminQuizReviewActionSerializer(serializers.Serializer):
    ACTION_APPROVE = "approve"
    ACTION_REJECT = "reject"

    action = serializers.ChoiceField(choices=[ACTION_APPROVE, ACTION_REJECT])
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["action"] == self.ACTION_REJECT and not attrs.get("reason", "").strip():
            raise ValidationError("A reason is required when rejecting a quiz.")
        return attrs


# =====================================================
# PUBLIC — the Quiz Hub (design_handoff_public_quiz_hub Phase 5)
#
# ⚠ EVERYTHING BELOW IS SERVED TO ANONYMOUS VISITORS. The admin/teacher
# serializers further up expose `is_correct` and `explanation`, which is
# correct for them and fatal here — a learner who can read the answer key
# before answering has no reason to answer. So these build on
# QuestionPublicSerializer / ChoicePublicSerializer, which omit both, and
# nothing here may be "reused" from the admin side to save a few lines.
# =====================================================

class PracticeSetCardSerializer(serializers.ModelSerializer):
    """The card on the hub's grid. No questions — a list of 20 sets must not
    drag 200 questions and their choices behind it."""

    subject = serializers.CharField(source="subject_tag.label", read_only=True)
    subject_slug = serializers.CharField(source="subject_tag.slug", read_only=True)
    exam = serializers.CharField(
        source="exam_tag.label", read_only=True, default=None)
    # The number it can ACTUALLY serve today, not the target. Advertising 10
    # and serving 3 is the specific lie this field exists to prevent.
    question_count = serializers.IntegerField(
        source="available_count", read_only=True)
    # Phase 6 made this real. It counts SUBMITTED attempts only — a row is
    # created the moment someone opens a set, and counting those would
    # advertise "312 attempts" for a set 312 people bounced off.
    # Annotated by the list view; the fallback keeps a bare instance usable.
    attempt_count = serializers.SerializerMethodField()

    class Meta:
        model = PracticeSet
        fields = [
            "id", "slug", "title", "description", "subject", "subject_slug",
            "exam", "difficulty", "minutes", "question_count", "attempt_count",
            # Lets the card's "New this week" badge be a real fact rather than
            # the fixture's hand-set `fresh` flag.
            "created_at",
        ]

    def get_attempt_count(self, obj):
        cached = getattr(obj, "submitted_attempts", None)
        if cached is not None:
            return cached
        return obj.attempts.filter(submitted_at__isnull=False).count()


class PracticeSetDetailSerializer(PracticeSetCardSerializer):
    """The set plus the paper. Still no answers and no explanations."""

    questions = serializers.SerializerMethodField()

    class Meta(PracticeSetCardSerializer.Meta):
        fields = PracticeSetCardSerializer.Meta.fields + ["questions"]

    def get_questions(self, obj):
        return QuestionPublicSerializer(obj.pick_questions(), many=True).data


class PublicRailSerializer(serializers.ModelSerializer):
    """A subject or exam chip on the hub.

    `status` is NOT exposed — the public site has no business knowing what an
    admin intended. It gets `effective_status`, which is the server's verdict
    after the degrade rule (a `live` tag with nothing publishable comes back
    `soon`), so a chip can never be clickable onto an empty grid.
    """

    status = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    set_count = serializers.SerializerMethodField()

    class Meta:
        model = QuestionTag
        fields = ["id", "kind", "label", "slug", "status", "question_count",
                  "set_count", "icon", "color", "cover_image", "display_order"]

    def get_set_count(self, obj):
        """Published sets on this subject. Annotated by the view in one query.

        The subject tile advertises this, so it counts PUBLISHED sets only —
        a tile reading "6 sets" that opens onto three is the same class of
        lie as `question_count` advertising a target it cannot serve.
        """
        cached = getattr(obj, "published_set_count", None)
        return cached if cached is not None else 0

    def get_cover_image(self, obj):
        """Absolute URL of the tag's cover art, or None.

        None is a real answer, not a failure: the hub falls back to the tag's
        `color` as a flat tile. Returning a broken path instead would render
        an empty frame on the most visible part of the page.
        """
        image = obj.cover_image
        if not image or not image.file:
            return None
        url = image.file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def _count(self, obj):
        # Annotated by the view in one query; the fallback keeps the
        # serializer usable on a bare instance (e.g. in tests).
        cached = getattr(obj, "publishable_count", None)
        if cached is not None:
            return cached
        return Question.objects.publishable().filter(
            quiz__isnull=True, tags=obj).distinct().count()

    def get_question_count(self, obj):
        return self._count(obj)

    def get_status(self, obj):
        return obj.effective_status(self._count(obj))


class PublicAttemptAnswerReviewSerializer(serializers.ModelSerializer):
    """One reviewed question. ⚠ REVEALS THE ANSWER — only ever nested inside
    PublicAttemptReviewSerializer, which the view returns exclusively for a
    SUBMITTED attempt."""

    question_id = serializers.UUIDField(source="question.id", read_only=True)
    text = serializers.CharField(source="question.text", read_only=True)
    explanation = serializers.CharField(
        source="question.explanation", read_only=True)
    choices = ChoicePublicSerializer(
        source="question.choices", many=True, read_only=True)
    correct_choice_id = serializers.SerializerMethodField()
    selected_choice_id = serializers.UUIDField(read_only=True, allow_null=True)
    was_blank = serializers.SerializerMethodField()

    class Meta:
        model = PublicAttemptAnswer
        fields = ["question_id", "order", "text", "choices", "explanation",
                  "selected_choice_id", "selected_text", "correct_choice_id",
                  "is_correct", "was_blank"]

    def get_correct_choice_id(self, obj):
        correct = next(
            (c for c in obj.question.choices.all() if c.is_correct), None)
        return str(correct.id) if correct else None

    def get_was_blank(self, obj):
        """Distinguishes "left blank" from "picked an option that has since
        been edited away" — both have a NULL selected_choice, and conflating
        them would tell a learner they skipped a question they answered."""
        return obj.selected_choice_id is None and not obj.selected_text


class PublicAttemptReviewSerializer(serializers.ModelSerializer):
    set_title = serializers.CharField(
        source="practice_set.title", read_only=True)
    set_slug = serializers.CharField(source="practice_set.slug", read_only=True)
    answers = PublicAttemptAnswerReviewSerializer(many=True, read_only=True)

    class Meta:
        model = PublicAttempt
        fields = ["id", "set_title", "set_slug", "score", "total",
                  "started_at", "submitted_at", "answers"]


class PublicAttemptSubmitSerializer(serializers.Serializer):
    """`answers` maps question id → chosen choice id, or null for blank.

    A question the client omits entirely is treated as blank, so a learner
    who closes the tab half way still gets a scored, reviewable attempt
    rather than an error.
    """

    answers = serializers.ListField(child=serializers.DictField(), default=list)

    def validate_answers(self, rows):
        cleaned = []
        for row in rows:
            qid = row.get("question")
            if not qid:
                raise ValidationError(
                    "Every answer needs a `question` id.")
            cleaned.append({"question": str(qid),
                            "choice": row.get("choice") or None})
        return cleaned


class AdminPracticeSetSerializer(serializers.ModelSerializer):
    """Author a public practice set.

    ⚠ A set does not hold questions — it holds the CRITERIA that select them
    (see PracticeSet's docstring). So the admin is choosing a subject, an
    optional exam and difficulty, and a size; the paper follows from the
    bank. `available_count` is therefore the number that matters on screen,
    and it moves on its own as curation lands.
    """

    subject = serializers.CharField(source="subject_tag.label", read_only=True)
    exam = serializers.CharField(
        source="exam_tag.label", read_only=True, default=None)
    # What it can serve RIGHT NOW, which is not `question_count` (the target).
    available_count = serializers.IntegerField(read_only=True)
    attempt_count = serializers.SerializerMethodField()

    class Meta:
        model = PracticeSet
        fields = [
            "id", "slug", "title", "description", "subject_tag", "subject",
            "exam_tag", "exam", "difficulty", "question_count", "minutes",
            "seed", "status", "display_order", "created_at",
            "available_count", "attempt_count",
        ]
        read_only_fields = ["id", "slug", "created_at"]

    def get_attempt_count(self, obj):
        return obj.attempts.filter(submitted_at__isnull=False).count()

    def validate_subject_tag(self, tag):
        if tag.kind != QuestionTag.KIND_SUBJECT:
            raise ValidationError(
                f'"{tag.label}" is a {tag.kind} label, not a subject.')
        return tag

    def validate_exam_tag(self, tag):
        if tag is not None and tag.kind != QuestionTag.KIND_EXAM:
            raise ValidationError(
                f'"{tag.label}" is a {tag.kind} label, not an exam.')
        return tag

    def validate(self, attrs):
        """Refuse to PUBLISH a set that would serve nothing.

        Same principle as QuestionTag's degrade rule: an admin may not put
        something on the public page that opens empty. Here it has to be a
        hard refusal rather than a silent downgrade, because unlike a chip a
        set has no "Soon" state — it is either on the page or it is not.

        Evaluated against the MERGED result, not the incoming payload, so
        changing difficulty alone on an existing published set is checked
        against the combination that will actually be stored.
        """
        merged = PracticeSet(
            subject_tag=attrs.get(
                "subject_tag", getattr(self.instance, "subject_tag", None)),
            exam_tag=attrs.get(
                "exam_tag", getattr(self.instance, "exam_tag", None)),
            difficulty=attrs.get(
                "difficulty", getattr(self.instance, "difficulty", "")),
            question_count=attrs.get(
                "question_count", getattr(self.instance, "question_count", 10)),
        )
        status_value = attrs.get(
            "status", getattr(self.instance, "status", PracticeSet.STATUS_DRAFT))
        if status_value == PracticeSet.STATUS_PUBLISHED:
            if merged.subject_tag is None:
                raise ValidationError({"subject_tag": "Pick a subject."})
            if merged.question_queryset().count() == 0:
                raise ValidationError({
                    "status":
                        "Nothing in the bank matches this yet, so publishing "
                        "it would put an empty set on the site. Accept some "
                        "questions for this subject first, or save it as a "
                        "draft.",
                })
        return attrs
