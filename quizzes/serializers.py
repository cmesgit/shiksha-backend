from .models import Quiz

from django.db.models import Avg, Max, Min, Count
import uuid
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied

from courses.models import SubjectTeacher
from enrollments.models import Enrollment

from .models import (
    Quiz,
    Question,
    Choice,
    QuizAttempt,
    StudentAnswer,
)


# =====================================================
# CHOICE SERIALIZERS
# =====================================================

class ChoiceAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text", "is_correct"]


class ChoicePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Choice
        fields = ["id", "text"]


# =====================================================
# QUESTION CREATION
# =====================================================

class QuestionCreateSerializer(serializers.ModelSerializer):
    choices = ChoiceAdminSerializer(many=True)

    class Meta:
        model = Question
        fields = ["id", "text", "marks", "order", "choices", "explanation"]
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

        question = Question.objects.create(
            quiz=quiz,
            **validated_data
        )

        Choice.objects.bulk_create([
            Choice(question=question, **choice)
            for choice in choices_data
        ])

        return question


# =====================================================
# QUIZ CREATION
# =====================================================

class QuizCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Quiz
        fields = [
            "id",
            "subject",
            "title",
            "description",
            "due_date",
            "time_limit_minutes",
        ]
        read_only_fields = ["id"]

    def validate_subject(self, subject):
        user = self.context["request"].user

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        if not SubjectTeacher.objects.filter(
            subject=subject,
            teacher=user
        ).exists():
            raise PermissionDenied("You are not assigned to this subject.")

        return subject

    def validate_due_date(self, due_date):
        if due_date <= timezone.now():
            raise ValidationError("Due date must be in the future.")
        return due_date

    def create(self, validated_data):
        return Quiz.objects.create(
            created_by=self.context["request"].user,
            **validated_data
        )


# =====================================================
# STUDENT QUIZ DASHBOARD SERIALIZER
# =====================================================

class QuizDashboardSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)

    # resolved via annotation in the view
    questions_count = serializers.IntegerField(read_only=True)

    status = serializers.SerializerMethodField()
    score = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "subject_name",
            "course_title",
            "teacher_name",
            "created_at",
            "due_date",
            "total_marks",
            "questions_count",
            "status",
            "score",
            "is_published",
        ]

    def get_status(self, obj):
        # view prefetches attempts as "user_attempts"
        attempts = getattr(obj, "user_attempts", [])
        if not attempts:
            return "PENDING"
        return attempts[0].status

    def get_score(self, obj):
        # view prefetches submitted attempts as "user_submitted_attempts"
        attempts = getattr(obj, "user_submitted_attempts", [])
        if not attempts:
            return None
        return attempts[0].score


# =====================================================
# QUIZ SUBMISSION
# =====================================================

class QuizSubmitSerializer(serializers.Serializer):
    answers = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False
    )

    def validate(self, attrs):
        quiz = self.context["quiz"]
        user = self.context["request"].user

        if not Enrollment.objects.filter(
            user=user,
            course=quiz.subject.course,
            status=Enrollment.STATUS_ACTIVE
        ).exists():
            raise ValidationError("Not enrolled in this course.")

        if not quiz.is_published:
            raise ValidationError("Quiz not published.")

        if quiz.due_date <= timezone.now():
            raise ValidationError("Quiz expired.")

        if len(attrs["answers"]) != quiz.questions.count():
            raise ValidationError("All questions must be answered.")

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
            raise ValidationError("No active attempt found.")

        score = 0
        attempt.answers.all().delete()

        for item in submitted_answers:
            question_id = item.get("question")
            choice_id = item.get("selected_choice")

            question = Question.objects.filter(
                id=question_id,
                quiz=quiz
            ).first()

            if not question:
                raise ValidationError("Invalid question.")

            choice = Choice.objects.filter(
                id=choice_id,
                question=question
            ).first()

            if not choice:
                raise ValidationError("Invalid choice.")

            if choice.is_correct:
                score += question.marks

            StudentAnswer.objects.create(
                attempt=attempt,
                question=question,
                selected_choice=choice,
                is_correct=choice.is_correct,
            )

        attempt.score = score
        attempt.status = QuizAttempt.STATUS_SUBMITTED
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["score", "status", "submitted_at"])

        return attempt


# =====================================================
# QUIZ DETAIL (FOR TAKING QUIZ)
# =====================================================

class QuestionPublicSerializer(serializers.ModelSerializer):
    choices = ChoicePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = [
            "id",
            "text",
            "marks",
            "order",
            "choices",
            "explanation",
        ]


class QuizDetailSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)
    teacher_name = serializers.CharField(
        source="created_by.email", read_only=True)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "description",
            "subject_name",
            "course_title",
            "teacher_name",
            "due_date",
            "created_at",
            "time_limit_minutes",
            "questions",
        ]

    def get_questions(self, obj):
        questions = obj.questions.all().order_by("order")
        return QuestionPublicSerializer(questions, many=True).data


# =====================================================
# QUIZ RESULT SERIALIZERS
# =====================================================

class QuestionResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    text = serializers.CharField()
    selected_choice = serializers.CharField()
    correct_choice = serializers.CharField()
    is_correct = serializers.BooleanField()
    explanation = serializers.CharField(
        allow_blank=True, default="No explanation")


class QuizResultSerializer(serializers.Serializer):
    quiz_id = serializers.UUIDField()
    title = serializers.CharField()
    subject_name = serializers.CharField()
    teacher_name = serializers.CharField()
    total_marks = serializers.IntegerField()
    score = serializers.IntegerField()
    submitted_at = serializers.DateTimeField()
    questions = QuestionResultSerializer(many=True)


class TeacherQuizAttemptSerializer(serializers.ModelSerializer):
    student_id = serializers.UUIDField(source="student.id", read_only=True)
    student_email = serializers.EmailField(
        source="student.email", read_only=True)
    student_name = serializers.CharField(
        source="student.profile.full_name", read_only=True)
    total_marks = serializers.IntegerField(
        source="quiz.total_marks", read_only=True)

    class Meta:
        model = QuizAttempt
        fields = [
            "id",
            "student_id",
            "student_email",
            "student_name",
            "score",
            "total_marks",
            "submitted_at",
        ]


class TeacherQuizAnalyticsSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.CharField(
        source="subject.course.title", read_only=True)

    # all resolved via annotations in the view
    questions_count = serializers.IntegerField(read_only=True)
    total_attempts = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    highest_score = serializers.FloatField(read_only=True)
    lowest_score = serializers.FloatField(read_only=True)
    submission_rate = serializers.FloatField(read_only=True)

    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "created_at",
            "subject_name",
            "course_title",
            "due_date",
            "is_published",
            "is_expired",
            "questions_count",
            "total_attempts",
            "submission_rate",
            "average_score",
            "highest_score",
            "lowest_score",
        ]

    def get_is_expired(self, obj):
        return obj.due_date <= timezone.now()


class TeacherQuizStudentSummarySerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    student_name = serializers.CharField()
    student_email = serializers.EmailField()
    latest_submitted_at = serializers.DateTimeField()
    best_score = serializers.FloatField()
    total_marks = serializers.IntegerField()
    attempts_count = serializers.IntegerField()
