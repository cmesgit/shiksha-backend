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
    TeacherQuizAttemptSerializer,
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


class TeacherDeleteQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def delete(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"),
            pk=pk
        )

        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        if quiz.created_by != request.user:
            raise PermissionDenied("You did not create this quiz.")

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

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        subject = get_object_or_404(
            Subject.objects.select_related("course"),
            id=subject_id
        )

        if not subject.subject_teachers.filter(teacher=user).exists():
            raise PermissionDenied("Not assigned to this subject.")

        enrolled_count_subquery = (
            subject.course.enrollments.filter(status="ACTIVE").count()
        )

        return (
            Quiz.objects
            .filter(subject=subject)
            .select_related("subject", "subject__course")
            .annotate(
                total_attempts=Count("attempts", distinct=True),
                average_score=Avg("attempts__score"),
                highest_score=Max("attempts__score"),
                lowest_score=Min("attempts__score"),
                questions_count=Count("questions", distinct=True),
                submission_rate=Count(
                    "attempts", distinct=True) * 100.0 / (enrolled_count_subquery or 1),
            )
            .order_by("-created_at")
        )


# =====================================================
# STUDENT VIEWS
# =====================================================

class StudentDashboardView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        status_filter = request.query_params.get("status")
        subject_id = request.query_params.get("subject")

        user = request.user

        quizzes = (
            Quiz.objects
            .filter(
                subject__course__enrollments__user=user,
                subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
                is_published=True,
            )
            .select_related("subject", "subject__course", "created_by")
            .annotate(questions_count=Count("questions", distinct=True))
            .prefetch_related(
                Prefetch(
                    "attempts",
                    queryset=QuizAttempt.objects.filter(
                        student=user
                    ).order_by("-attempt_number"),
                    to_attr="user_attempts",
                ),
                Prefetch(
                    "attempts",
                    queryset=QuizAttempt.objects.filter(
                        student=user,
                        status=QuizAttempt.STATUS_SUBMITTED,
                    ).order_by("-attempt_number"),
                    to_attr="user_submitted_attempts",
                ),
            )
            .distinct()
        )

        if subject_id:
            quizzes = quizzes.filter(subject_id=subject_id)

        submitted_ids = QuizAttempt.objects.filter(
            student=user,
            status=QuizAttempt.STATUS_SUBMITTED,
        ).values_list("quiz_id", flat=True).distinct()

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

        last_attempt = QuizAttempt.objects.filter(
            quiz=quiz,
            student=request.user
        ).order_by("-attempt_number").first()

        new_attempt_number = (
            last_attempt.attempt_number + 1) if last_attempt else 1

        QuizAttempt.objects.create(
            quiz=quiz,
            student=request.user,
            attempt_number=new_attempt_number
        )

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
            .select_related("subject", "subject__course", "created_by")
            .prefetch_related("questions__choices"),
            pk=pk,
            is_published=True,
        )

        if request.user.has_role("TEACHER"):
            if quiz.created_by != request.user:
                raise PermissionDenied("Not authorized for this quiz.")
        elif not Enrollment.objects.filter(
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
                "subject", "subject__course", "created_by"),
            pk=pk,
        )

        attempt = (
            QuizAttempt.objects
            .filter(
                quiz=quiz,
                student=request.user,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
            .prefetch_related(
                "answers__question__choices",
                "answers__selected_choice",
            )
            .order_by("-attempt_number")
            .first()
        )

        if not attempt:
            raise ValidationError("No submitted attempt found.")

        result_questions = []

        for answer in attempt.answers.all():
            correct_choice = next(
                (c for c in answer.question.choices.all() if c.is_correct),
                None
            )
            result_questions.append({
                "id": answer.question.id,
                "text": answer.question.text,
                "selected_choice": answer.selected_choice.text,
                "correct_choice": correct_choice.text if correct_choice else "",
                "is_correct": answer.is_correct,
                "explanation": answer.question.explanation,
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
        quizzes = (
            Quiz.objects
            .filter(
                is_published=True,
                subject__course__enrollments__user=request.user,
                subject__course__enrollments__status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("subject", "created_by")
            .distinct()
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


class TeacherQuizAttemptsView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        user = request.user

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"),
            id=pk
        )

        if not SubjectTeacher.objects.filter(
            subject=quiz.subject,
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        attempts = (
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .select_related("student", "student__profile", "quiz")
            .order_by("student_id", "-attempt_number")
        )

        student_map = {}

        for attempt in attempts:
            student_id = str(attempt.student.id)

            if student_id not in student_map:
                student_map[student_id] = {
                    "student_id": attempt.student.id,
                    "student_name": attempt.student.profile.full_name,
                    "student_email": attempt.student.email,
                    "latest_submitted_at": attempt.submitted_at,
                    "best_score": attempt.score,
                    "attempts_count": 1,
                    "total_marks": attempt.quiz.total_marks,
                }
            else:
                student_data = student_map[student_id]
                if attempt.submitted_at > student_data["latest_submitted_at"]:
                    student_data["latest_submitted_at"] = attempt.submitted_at
                if attempt.score > student_data["best_score"]:
                    student_data["best_score"] = attempt.score
                student_data["attempts_count"] += 1

        return Response(list(student_map.values()))


class TeacherStudentAttemptsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]
    serializer_class = TeacherQuizAttemptSerializer

    def get_queryset(self):
        user = self.request.user
        quiz_id = self.kwargs["quiz_id"]
        student_id = self.kwargs["student_id"]

        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"),
            id=quiz_id
        )

        if not SubjectTeacher.objects.filter(
            subject=quiz.subject,
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        return (
            QuizAttempt.objects
            .filter(
                quiz=quiz,
                student_id=student_id,
                status=QuizAttempt.STATUS_SUBMITTED
            )
            .select_related("student", "student__profile")
            .order_by("attempt_number")
        )


class TeacherQuizAttemptDetailView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        attempt = get_object_or_404(
            QuizAttempt.objects
            .select_related("student__profile", "quiz")
            .prefetch_related(
                "answers__question__choices",
                "answers__selected_choice",
            ),
            id=pk
        )

        if not SubjectTeacher.objects.filter(
            subject=attempt.quiz.subject,
            teacher=request.user
        ).exists():
            raise PermissionDenied("Not authorized.")

        result_questions = []

        for answer in attempt.answers.all():
            correct_choice = next(
                (c for c in answer.question.choices.all() if c.is_correct),
                None
            )
            result_questions.append({
                "question": answer.question.text,
                "options": [c.text for c in answer.question.choices.all()],
                "selected": answer.selected_choice.text,
                "correct": correct_choice.text if correct_choice else "",
            })

        return Response({
            "student_name": attempt.student.profile.full_name,
            "score": attempt.score,
            "total": attempt.quiz.total_marks,
            "submitted_at": attempt.submitted_at,
            "questions": result_questions,
        })
