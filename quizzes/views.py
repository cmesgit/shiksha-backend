from django.db.models import Prefetch
from rest_framework.exceptions import PermissionDenied
from rest_framework import generics
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError

from accounts.permissions import IsEmailVerified
from enrollments.models import Enrollment
from django.db import models
from django.db.models import Count, Avg, Max, Min
from courses.models import Subject, SubjectTeacher

from .models import Quiz, QuizAttempt
from .serializers import (
    QuizCreateSerializer,
    QuestionCreateSerializer,
    QuizDashboardSerializer,
    QuizSubmitSerializer,
    QuizDetailSerializer,
    QuizResultSerializer,
    TeacherQuizAnalyticsSerializer,

)


# =====================================================
# TEACHER VIEWS
# =====================================================

class CreateQuizView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

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
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

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
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized.")

        if quiz.is_published:
            raise ValidationError("Quiz already published.")

        if not quiz.questions.exists():
            raise ValidationError("Cannot publish empty quiz.")

        total_marks = quiz.questions.aggregate(
            total=models.Sum("marks")
        )["total"] or 0

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
        subject_id = request.query_params.get("subject")

        quizzes = Quiz.objects.filter(
            subject__course__enrollments__user=request.user,
            subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
            is_published=True,
        ).select_related(
            "subject",
            "subject__course",
            "created_by",
        ).distinct()

        if subject_id:
            quizzes = quizzes.filter(subject_id=subject_id)

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
            is_published=True,
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
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects
            .select_related(
                "subject",
                "subject__course",
                "created_by",
            )
            .prefetch_related("questions__choices"),
            pk=pk,
            is_published=True,
        )

        if not Enrollment.objects.filter(
            user=request.user,
            course=quiz.subject.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists():
            raise ValidationError("Not enrolled in this course.")

        serializer = QuizDetailSerializer(
            quiz,
            context={"request": request},
        )

        return Response(serializer.data)


class QuizResultView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related(
                "subject",
                "subject__course",
                "created_by",
            ),
            pk=pk,
        )

        attempt = get_object_or_404(
            QuizAttempt.objects.prefetch_related(
                "answers__question",
                "answers__selected_choice",
                "answers__question__choices",
            ),
            quiz=quiz,
            student=request.user,
            status=QuizAttempt.STATUS_SUBMITTED,
        )

        result_questions = []

        for answer in attempt.answers.all():
            correct_choice = answer.question.choices.filter(
                is_correct=True
            ).first()

            result_questions.append({
                "id": answer.question.id,
                "text": answer.question.text,
                "selected_choice": answer.selected_choice.text,
                "correct_choice": correct_choice.text if correct_choice else "",
                "is_correct": answer.is_correct,
            })

        data = {
            "quiz_id": quiz.id,
            "title": quiz.title,
            "subject_name": quiz.subject.name,
            "teacher_name": quiz.created_by.email,
            "total_marks": quiz.total_marks,
            "score": attempt.score,
            "submitted_at": attempt.submitted_at,
            "questions": result_questions,
        }

        serializer = QuizResultSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        return Response(serializer.data)


class StudentQuizSubjectsView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        quizzes = Quiz.objects.filter(
            is_published=True,
            subject__course__enrollments__user=request.user,
            subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
        ).select_related(
            "subject",
            "created_by",
        )

        subjects_map = {}

        for quiz in quizzes:
            subject = quiz.subject

            if subject.id not in subjects_map:
                subjects_map[subject.id] = {
                    "id": subject.id,
                    "subject": subject.name,
                    "teacher": quiz.created_by.email,
                }

        return Response(list(subjects_map.values()))


class TeacherSubjectQuizListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = QuizDashboardSerializer

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        subject = get_object_or_404(Subject, id=subject_id)

        if not SubjectTeacher.objects.filter(
            subject=subject,
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        return Quiz.objects.filter(
            subject=subject
        ).order_by("-created_at")


class TeacherDeleteQuizView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized.")

        if quiz.attempts.exists():
            raise ValidationError(
                "Cannot delete quiz with attempts."
            )

        quiz.delete()

        return Response(
            {"detail": "Quiz deleted successfully."},
            status=status.HTTP_204_NO_CONTENT,
        )


class TeacherQuizAttemptsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = QuizResultSerializer

    def get_queryset(self):
        if not self.request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(
            Quiz.objects.prefetch_related(
                "attempts__student"
            ),
            pk=self.kwargs["pk"]
        )

        if quiz.created_by != self.request.user:
            raise PermissionDenied("Not authorized.")

        return quiz.attempts.filter(
            status=QuizAttempt.STATUS_SUBMITTED
        )


class TeacherDeleteQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def delete(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"),
            pk=pk
        )

        # 🔐 Role check
        if not request.user.has_role("teacher"):
            raise PermissionDenied("Only teachers allowed.")

        # 🔐 Ownership check
        if quiz.created_by != request.user:
            raise PermissionDenied("You did not create this quiz.")

        # 🔐 Prevent deleting published quizzes with attempts
        if quiz.is_published and quiz.attempts.exists():
            return Response(
                {"detail": "Cannot delete quiz with student attempts."},
                status=status.HTTP_400_BAD_REQUEST
            )

        quiz.delete()

        return Response(
            {"detail": "Quiz deleted successfully."},
            status=status.HTTP_204_NO_CONTENT
        )


class TeacherSubjectQuizListView(generics.ListAPIView):
    serializer_class = TeacherQuizAnalyticsSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        # 🔐 Role Protection
        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        # 🔐 Ensure teacher assigned to subject
        subject = get_object_or_404(
            Subject.objects.select_related("course"),
            id=subject_id
        )

        if not subject.subject_teachers.filter(
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        # 📊 Annotate analytics
        return (
            Quiz.objects
            .filter(subject=subject)
            .select_related("subject", "subject__course")
            .annotate(
                total_attempts=Count("attempts"),
                average_score=Avg("attempts__score"),
                highest_score=Max("attempts__score"),
                lowest_score=Min("attempts__score"),
            )
            .order_by("-created_at")
        )


class TeacherQuizAttemptsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get_queryset(self):
        user = self.request.user
        quiz_id = self.kwargs["pk"]

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"),
            id=quiz_id
        )

        # ensure teacher owns subject
        if not SubjectTeacher.objects.filter(
            subject=quiz.subject,
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        return (
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .select_related("student", "student__profile")
            .order_by("-submitted_at")
        )
