from .models import Quiz

from datetime import timedelta
from decimal import Decimal
from django.db.models import Avg, Max, Min, Count
import uuid
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied

from courses.board_display import board_name_via
from courses.models import Batch, Chapter
from courses.services import is_teacher_of, resolve_or_create_chapter, teaches_subject
from courses.chapter_tags import ChapterTagWriteMixin, serialize_tags
from enrollments.models import Enrollment

from .models import (
    Quiz,
    QuizSection,
    Question,
    Choice,
    QuizAttempt,
    StudentAnswer,
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
    batch_id = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(), source="batch", write_only=True,
    )
    # Existing chapter, or type a new one via custom_chapter — resolved in
    # validate() via resolve_or_create_chapter(), same as assignments/materials.
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(), source="chapter",
        write_only=True, required=False,
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
        batch = attrs.get("batch")
        chapter = attrs.get("chapter")
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
                attrs["chapter"] = chapter
        elif subject and chapter.subject_id != subject.id:
            raise ValidationError(
                {"chapter_id": "Pick a chapter from this quiz's own subject."}
            )

        self._tag_subject = subject
        return attrs

    def _apply_tags(self, quiz):
        tags, save_to_course, present = getattr(
            self, "_tag_input", ([], False, False)
        )
        return self.apply_chapter_tags(
            quiz, getattr(self, "_tag_subject", None) or quiz.subject,
            tags, save_to_course, present,
        )

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
        return self._apply_tags(Quiz.objects.create(
            created_by=self.context["request"].user,
            **validated_data
        ))

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

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "subject_id", "subject_name", "course_title",
            "board_name", "teacher_name",
            "created_at", "total_marks", "questions_count", "time_limit_minutes",
            "status", "score", "best_score", "is_published", "attempts_count", "quiz_type",
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
            "is_published", "questions", "quiz_type", "review_status",
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
    selected_choice = serializers.CharField()
    correct_choice = serializers.CharField(allow_blank=True, default="")
    is_correct = serializers.BooleanField()
    explanation = serializers.CharField(
        allow_blank=True, default="No explanation")
    topic = serializers.CharField(allow_blank=True, default="")
    difficulty = serializers.CharField(default="medium")
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
    teacher_name = serializers.CharField()
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
            "is_published", "is_assigned", "questions_count",
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
    quiz_id = serializers.UUIDField(source="quiz.id", read_only=True)
    quiz_title = serializers.CharField(source="quiz.title", read_only=True)
    subject_id = serializers.UUIDField(source="quiz.subject.id", read_only=True)
    subject_name = serializers.CharField(source="quiz.subject.name", read_only=True)
    author_name = serializers.CharField(
        source="quiz.created_by.email", read_only=True, default=None)
    author_id = serializers.UUIDField(source="quiz.created_by.id", read_only=True, default=None)

    class Meta:
        model = Question
        fields = [
            "id", "text", "marks", "explanation", "topic", "difficulty",
            "choices", "quiz_id", "quiz_title", "subject_id", "subject_name",
            "author_name", "author_id", "created_at",
            # Phase 2 additions — purely additive, see class docstring.
            "bank_state", "suggest_to_bank", "bank_feedback",
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
            "quiz_type", "review_status", "is_published", "is_assigned",
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
            "is_published", "is_assigned", "total_marks", "time_limit_minutes",
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
