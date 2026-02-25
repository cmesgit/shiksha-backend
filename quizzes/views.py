from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError

from accounts.permissions import IsEmailVerified
from enrollments.models import Enrollment

from .models import Quiz, QuizAttempt
from .serializers import (
    QuizCreateSerializer,
    QuestionCreateSerializer,
    QuizDashboardSerializer,
    QuizSubmitSerializer,
)


# =====================================================
# TEACHER VIEWS
# =====================================================

class CreateQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        if not request.user.has_role("teacher"):
            raise ValidationError("Only teachers can create quizzes.")

        subject_id = request.data.get("subject")
        if not subject_id:
            raise ValidationError("Subject is required.")

        if not SubjectTeacher.objects.filter(
            subject_id=subject_id,
            teacher=request.user
        ).exists():
            raise ValidationError("You are not assigned to this subject.")

        serializer = QuizCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        quiz = serializer.save()

        return Response(
            {"id": quiz.id, "detail": "Quiz created successfully."},
            status=status.HTTP_201_CREATED,
        )


class AddQuestionView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        quiz = get_object_or_404(Quiz, pk=pk)

        if not request.user.has_role("teacher"):
            raise ValidationError("Only teachers allowed.")

        if quiz.created_by != request.user:
            raise ValidationError("Not authorized for this quiz.")

        if quiz.is_published:
            raise ValidationError("Cannot modify published quiz.")

        serializer = QuestionCreateSerializer(
            data=request.data,
            context={"quiz": quiz},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"detail": "Question added successfully."},
            status=status.HTTP_201_CREATED,
        )


class PublishQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def patch(self, request, pk):
        quiz = get_object_or_404(Quiz, pk=pk)

        if not request.user.has_role("teacher"):
            raise ValidationError("Only teachers allowed.")

        if quiz.created_by != request.user:
            raise ValidationError("Not authorized.")

        if quiz.is_published:
            raise ValidationError("Quiz already published.")

        if quiz.questions.count() == 0:
            raise ValidationError("Cannot publish empty quiz.")

        total_marks = sum(q.marks for q in quiz.questions.all())

        quiz.total_marks = total_marks
        quiz.is_published = True
        quiz.save(update_fields=["total_marks", "is_published"])

        return Response(
            {"detail": "Quiz published successfully."},
            status=status.HTTP_200_OK,
        )


# =====================================================
# STUDENT VIEWS
# =====================================================

class StudentDashboardView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        status_filter = request.query_params.get("status")

        quizzes = Quiz.objects.filter(
            subject__course__enrollments__user=request.user,
            subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
            is_published=True,
        ).select_related(
            "subject",
            "subject__course",
            "created_by",
            "created_by__profile",
        ).distinct()

        submitted_ids = QuizAttempt.objects.filter(
            student=request.user,
            status=QuizAttempt.STATUS_SUBMITTED,
        ).values_list("quiz_id", flat=True)

        if status_filter == "completed":
            quizzes = quizzes.filter(id__in=submitted_ids)
        elif status_filter == "pending":
            quizzes = quizzes.exclude(id__in=submitted_ids)

        serializer = QuizDashboardSerializer(
            quizzes,
            many=True,
            context={"request": request},
        )

        return Response(serializer.data)


class StartQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject__course"),
            pk=pk,
            is_published=True,
        )

        if not Enrollment.objects.filter(
            user=request.user,
            course=quiz.subject.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists():
            raise ValidationError("Not enrolled in this course.")

        if quiz.due_date <= timezone.now():
            raise ValidationError("Quiz expired.")

        attempt, _ = QuizAttempt.objects.get_or_create(
            quiz=quiz,
            student=request.user,
        )

        if attempt.status == QuizAttempt.STATUS_SUBMITTED:
            raise ValidationError("Quiz already submitted.")

        return Response(
            {"detail": "Quiz started successfully."},
            status=status.HTTP_200_OK,
        )


class SubmitQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject__course"),
            pk=pk,
        )

        serializer = QuizSubmitSerializer(
            data=request.data,
            context={"request": request, "quiz": quiz},
        )
        serializer.is_valid(raise_exception=True)

        attempt = serializer.save()

        return Response(
            {
                "detail": "Quiz submitted successfully.",
                "score": attempt.score,
                "total_marks": quiz.total_marks,
            },
            status=status.HTTP_200_OK,
        )


class QuizDetailView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsEmailVerified,
    ]

    def get(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects
            .select_related(
                "subject",
                "subject__course",
                "created_by",
                "created_by__profile",
            )
            .prefetch_related(
                "questions__choices"
            ),
            pk=pk,
            is_published=True,
        )

        # Enrollment check
        if not Enrollment.objects.filter(
            user=request.user,
            course=quiz.subject.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists():
            raise ValidationError("Not enrolled in this course.")

        # Expiry check
        if quiz.due_date <= timezone.now():
            raise ValidationError("Quiz expired.")

        serializer = QuizDetailSerializer(
            quiz,
            context={"request": request},
        )

        return Response(serializer.data)


class QuizResultView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsEmailVerified,
    ]

    def get(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related(
                "subject",
                "subject__course",
                "created_by",
                "created_by__profile",
            ),
            pk=pk,
        )

        try:
            attempt = QuizAttempt.objects.select_related(
                "quiz"
            ).prefetch_related(
                "answers__question",
                "answers__selected_choice",
            ).get(
                quiz=quiz,
                student=request.user,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
        except QuizAttempt.DoesNotExist:
            raise ValidationError("Quiz not submitted yet.")

        result_questions = []

        for answer in attempt.answers.all():
            correct_choice = answer.question.choices.get(is_correct=True)

            result_questions.append({
                "id": answer.question.id,
                "text": answer.question.text,
                "selected_choice": answer.selected_choice.text,
                "correct_choice": correct_choice.text,
                "is_correct": answer.is_correct,
            })

        data = {
            "quiz_id": quiz.id,
            "title": quiz.title,
            "subject_name": quiz.subject.name,
            "teacher_name": quiz.created_by.profile.full_name,
            "total_marks": quiz.total_marks,
            "score": attempt.score,
            "submitted_at": attempt.submitted_at,
            "questions": result_questions,
        }

        serializer = QuizResultSerializer(data)
        return Response(serializer.data)


class StudentQuizSubjectsView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsEmailVerified,
    ]

    def get(self, request):
        quizzes = (
            Quiz.objects
            .filter(
                is_published=True,
                subject__course__enrollments__user=request.user,
                subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
            )
            .select_related(
                "subject",
                "created_by",
                "created_by__profile",
            )
        )

        subjects_map = {}

        for quiz in quizzes:
            subject = quiz.subject

            if subject.id not in subjects_map:
                subjects_map[subject.id] = {
                    "id": subject.id,
                    "subject": subject.name,
                    "teacher": quiz.created_by.profile.full_name,
                }

        return Response(list(subjects_map.values()))
