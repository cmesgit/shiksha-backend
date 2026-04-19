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

        attempt_count = quiz.attempts.count()

        # If published and has attempts, require explicit confirmation via ?force=true
        if quiz.is_published and attempt_count > 0:
            force = request.query_params.get("force", "").lower() == "true"
            if not force:
                return Response(
                    {
                        "detail": f"This quiz has {attempt_count} student attempt(s). "
                        f"Deleting will permanently remove all attempts and scores. "
                        f"Pass ?force=true to confirm.",
                        "attempt_count": attempt_count,
                        "requires_force": True,
                    },
                    status=status.HTTP_409_CONFLICT
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

        submitted_ids = set(
            QuizAttempt.objects.filter(
                student=user,
                status=QuizAttempt.STATUS_SUBMITTED,
            ).values_list("quiz_id", flat=True).distinct()
        )

        if status_filter == "completed":
            quizzes = quizzes.filter(id__in=submitted_ids)
        elif status_filter == "pending":
            quizzes = quizzes.exclude(id__in=submitted_ids)

        serializer = QuizDashboardSerializer(
            quizzes,
            many=True,
            context={"request": request, "submitted_ids": submitted_ids},
        )

        return Response(serializer.data)


class StartQuizView(APIView):
    """
    POST /quizzes/:pk/start/

    Creates a new PENDING attempt for the student.

    Multiple attempts are allowed — each call creates a fresh attempt with
    an incremented attempt_number, UNLESS there is already an active
    (PENDING) attempt in progress, in which case we return the existing one
    to prevent ghost attempts from page refreshes.
    """
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

        # ── Key fix: reuse an existing PENDING attempt instead of creating a new one ──
        existing_pending = QuizAttempt.objects.filter(
            quiz=quiz,
            student=request.user,
            status=QuizAttempt.STATUS_PENDING,
        ).order_by("-attempt_number").first()

        if existing_pending:
            # Student refreshed the page or navigated back — resume the same attempt
            return Response(
                {"detail": "Resuming existing attempt.",
                    "attempt_id": existing_pending.id},
                status=status.HTTP_200_OK,
            )

        # Create a new attempt (first attempt or re-attempt after submitting)
        last_attempt = QuizAttempt.objects.filter(
            quiz=quiz,
            student=request.user
        ).order_by("-attempt_number").first()

        new_attempt_number = (
            last_attempt.attempt_number + 1) if last_attempt else 1

        new_attempt = QuizAttempt.objects.create(
            quiz=quiz,
            student=request.user,
            attempt_number=new_attempt_number
        )

        return Response(
            {"detail": "Quiz started successfully.", "attempt_id": new_attempt.id},
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


class QuizDetailDraftView(APIView):
    """
    GET /quizzes/:pk/draft/

    Teacher-only: returns full quiz data (including correct answers) for
    an UNPUBLISHED quiz so the teacher can preview before publishing.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers can preview drafts.")

        # Allow fetching whether published or not
        quiz = get_object_or_404(
            Quiz.objects
            .select_related("subject", "subject__course", "created_by")
            .prefetch_related("questions__choices"),
            pk=pk,
        )

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

        # Reuse the same serializer — teacher gets choices with is_correct
        from .serializers import QuizDetailTeacherSerializer
        serializer = QuizDetailTeacherSerializer(
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

        # Support ?attempt=<id> to view a specific attempt
        attempt_id = request.query_params.get("attempt")

        if attempt_id:
            attempt = get_object_or_404(
                QuizAttempt.objects
                .prefetch_related(
                    "answers__question__choices",
                    "answers__selected_choice",
                ),
                id=attempt_id,
                quiz=quiz,
                student=request.user,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
        else:
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
            "attempt_number": attempt.attempt_number,
            "questions": result_questions,
        }

        serializer = QuizResultSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        return Response(serializer.data)


class StudentQuizSubjectsView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        # FIX: query all enrolled subjects directly instead of filtering
        # through quizzes — so subjects without quizzes also appear
        subjects = (
            Subject.objects
            .filter(
                course__enrollments__user=request.user,
                course__enrollments__status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("course")
            .prefetch_related("subject_teachers__teacher")
            .distinct()
        )

        data = []
        for subject in subjects:
            teacher_rel = subject.subject_teachers.first()
            teacher_name = (
                teacher_rel.teacher.email if teacher_rel else ""
            )
            data.append({
                "id": subject.id,
                "subject": subject.name,
                "teacher": teacher_name,
            })

        return Response(data)


class StudentQuizAttemptsView(APIView):
    """
    GET /student/quizzes/:pk/attempts/

    Returns all SUBMITTED attempts for the current student on a given quiz.
    Used by QuizAttempts.jsx so students can review past attempts.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        quiz = get_object_or_404(Quiz, pk=pk, is_published=True)

        attempts = (
            QuizAttempt.objects
            .filter(
                quiz=quiz,
                student=request.user,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
            .select_related("student__profile")
            .order_by("attempt_number")
        )

        data = [
            {
                "id": a.id,
                "attempt_number": a.attempt_number,
                "student_name": (
                    a.student.profile.full_name
                    if hasattr(a.student, "profile") else a.student.email
                ),
                "submitted_at": a.submitted_at,
                "score": a.score,
                "total_marks": quiz.total_marks,
                "time_taken": None,
            }
            for a in attempts
        ]

        return Response({"title": quiz.title, "attempts": data})


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

        from django.db.models import Max, FloatField, ExpressionWrapper, F

        student_summaries = (
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .values("student_id", "student__profile__full_name", "student__email")
            .annotate(
                latest_submitted_at=Max("submitted_at"),
                best_score=Max("score"),
                average_score=Avg("score"),
                attempts_count=Count("id"),
            )
            .order_by("student__profile__full_name")
        )

        data = [
            {
                "student_id": s["student_id"],
                "student_name": s["student__profile__full_name"] or s["student__email"],
                "student_email": s["student__email"],
                "latest_submitted_at": s["latest_submitted_at"],
                "best_score": s["best_score"],
                "average_score": round(s["average_score"] or 0, 2),
                "attempts_count": s["attempts_count"],
                "total_marks": quiz.total_marks,
            }
            for s in student_summaries
        ]

        return Response(data)


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
            "attempt_number": attempt.attempt_number,
            "questions": result_questions,
        })
