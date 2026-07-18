from .models import Quiz

from django.db.models import Avg, Max, Min, Count
import uuid
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied

from courses.models import SubjectTeacher
from courses.services import teaches_subject
from enrollments.models import Enrollment

from .models import (
    Quiz,
    Question,
    Choice,
    QuizAttempt,
    StudentAnswer,
)


class ChoiceAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text", "is_correct"]


class ChoicePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text"]


class QuestionCreateSerializer(serializers.ModelSerializer):
    choices = ChoiceAdminSerializer(many=True)

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices",
                  "explanation", "topic", "difficulty"]
        read_only_fields = ["id"]

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


class QuizCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Quiz
        fields = ["id", "subject", "title",
                  "description", "time_limit_minutes", "quiz_type"]
        read_only_fields = ["id"]

    def validate_subject(self, subject):
        user = self.context["request"].user
        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")
        if not teaches_subject(user, subject):
            raise PermissionDenied("You are not assigned to this subject.")
        return subject

    def create(self, validated_data):
        return Quiz.objects.create(
            created_by=self.context["request"].user,
            **validated_data
        )


class QuizDashboardSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    questions_count = serializers.IntegerField(read_only=True)
    status = serializers.SerializerMethodField()
    score = serializers.SerializerMethodField()
    attempts_count = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "subject_name", "course_title", "teacher_name",
            "created_at", "total_marks", "questions_count", "time_limit_minutes",
            "status", "score", "is_published", "attempts_count", "quiz_type",
        ]

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

        if not quiz.is_published:
            raise ValidationError("Quiz not published.")

        # Allow partial submission (auto-submit on timer expiry)
        # We do NOT validate all questions answered here — partial is OK.
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        quiz = self.context["quiz"]
        user = self.context["request"].user
        submitted_answers = self.validated_data["answers"]

        attempt = QuizAttempt.objects.select_for_update().filter(
            quiz=quiz,
            student=user,
            status=QuizAttempt.STATUS_PENDING,
        ).order_by("-attempt_number").first()

        if not attempt:
            raise ValidationError(
                "No active attempt found. Please start the quiz first.")

        score = 0
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
                continue  # skip invalid choices gracefully

            if choice.is_correct:
                score += question.marks

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

        attempt.score = score
        attempt.status = QuizAttempt.STATUS_SUBMITTED
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["score", "status", "submitted_at"])
        return attempt


class QuestionPublicSerializer(serializers.ModelSerializer):
    choices = ChoicePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices", "topic", "difficulty"]
        # NOTE: explanation is intentionally omitted from the public serializer
        # so students don't see it before submitting


class QuestionTeacherSerializer(serializers.ModelSerializer):
    """Full question data including correct answers — for teacher draft preview."""
    choices = ChoiceAdminSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices",
                  "explanation", "topic", "difficulty"]


class QuizDetailSerializer(serializers.ModelSerializer):
    """Student-facing quiz detail — no correct answers exposed."""
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_name", "course_title",
            "teacher_name", "created_at", "time_limit_minutes", "questions",
            "quiz_type",
        ]

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionPublicSerializer(questions, many=True).data


class QuizDetailTeacherSerializer(serializers.ModelSerializer):
    """
    Teacher draft preview — includes correct answers and explanations.
    Used by QuizDetailDraftView for unpublished quiz review.
    """
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    questions = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_name", "course_title",
            "teacher_name", "created_at", "time_limit_minutes",
            "is_published", "questions", "quiz_type", "review_status",
            "review_note", "reviewed_at", "submitted_for_review_at",
            "is_editable",
        ]

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionTeacherSerializer(questions, many=True).data


class QuestionResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    text = serializers.CharField()
    selected_choice = serializers.CharField()
    correct_choice = serializers.CharField()
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
    teacher_name = serializers.CharField()
    quiz_type = serializers.CharField(default="mock")
    total_marks = serializers.IntegerField()
    score = serializers.IntegerField()
    submitted_at = serializers.DateTimeField()
    attempt_number = serializers.IntegerField(default=1)
    questions = QuestionResultSerializer(many=True)

    # ── analytics (results + analytics screen) ──────────────────────────
    class_avg_percent = serializers.FloatField(default=0)
    percentile = serializers.FloatField(default=0)
    topic_breakdown = TopicBreakdownSerializer(many=True, default=list)
    difficulty_breakdown = DifficultyBreakdownSerializer(many=True, default=list)
    score_trend = ScoreTrendPointSerializer(many=True, default=list)
    wrong_question_ids = serializers.ListField(
        child=serializers.UUIDField(), default=list)


class TeacherQuizAttemptSerializer(serializers.ModelSerializer):
    student_id = serializers.UUIDField(source="student.id", read_only=True)
    student_email = serializers.EmailField(
        source="student.email", read_only=True)
    student_name = serializers.CharField(
        source="student.username", read_only=True)
    total_marks = serializers.IntegerField(
        source="quiz.total_marks", read_only=True)

    class Meta:
        model = QuizAttempt
        fields = [
            "id", "student_id", "student_email", "student_name",
            "score", "total_marks", "submitted_at", "attempt_number",
        ]


class TeacherQuizAnalyticsSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    questions_count = serializers.IntegerField(read_only=True)
    total_attempts = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    highest_score = serializers.FloatField(read_only=True)
    lowest_score = serializers.FloatField(read_only=True)
    submission_rate = serializers.FloatField(read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "created_at", "subject_name", "course_title",
            "is_published", "questions_count",
            "total_attempts", "submission_rate", "average_score",
            "highest_score", "lowest_score", "quiz_type", "review_status",
            "review_note",
        ]


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
    """A question surfaced in the teacher question bank. The bank is not a
    separate table — it's a filtered view over questions that already
    belong to a *finalized* (admin-approved) quiz, so what you reuse has
    already been vetted."""
    choices = ChoiceAdminSerializer(many=True, read_only=True)
    quiz_id = serializers.UUIDField(source="quiz.id", read_only=True)
    quiz_title = serializers.CharField(source="quiz.title", read_only=True)
    subject_id = serializers.UUIDField(source="quiz.subject.id", read_only=True)
    subject_name = serializers.CharField(source="quiz.subject.name", read_only=True)
    author_name = serializers.CharField(
        source="quiz.created_by.email", read_only=True)
    author_id = serializers.UUIDField(source="quiz.created_by.id", read_only=True)

    class Meta:
        model = Question
        fields = [
            "id", "text", "marks", "explanation", "topic", "difficulty",
            "choices", "quiz_id", "quiz_title", "subject_id", "subject_name",
            "author_name", "author_id", "created_at",
        ]


# =====================================================
# ADMIN — academy quiz verification
# =====================================================

class AdminQuizListSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    questions_count = serializers.IntegerField(read_only=True)
    attempts_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "subject_name", "course_title", "teacher_name",
            "quiz_type", "review_status", "is_published", "questions_count",
            "attempts_count", "total_marks", "created_at",
            "submitted_for_review_at", "reviewed_at",
        ]


class AdminQuizDetailSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.email", read_only=True, default=None)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id", "title", "description", "subject_name", "course_title",
            "teacher_name", "quiz_type", "review_status", "review_note",
            "is_published", "total_marks", "time_limit_minutes",
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
