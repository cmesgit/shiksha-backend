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

from accounts.permissions import IsEmailVerified, IsAdmin
from enrollments.models import Enrollment
from django.db import models
from django.db.models import Count, Avg, Max, Min, Q

from courses.models import Subject, SubjectTeacher

from .models import Quiz, QuizAttempt, Question, Choice, StudentAnswer
from .serializers import (
    QuizCreateSerializer,
    QuestionCreateSerializer,
    BulkQuestionCreateSerializer,
    QuizDashboardSerializer,
    QuizSubmitSerializer,
    QuizDetailSerializer,
    QuizResultSerializer,
    TeacherQuizAnalyticsSerializer,
    TeacherQuizAttemptSerializer,
    BankQuestionSerializer,
    AdminQuizListSerializer,
    AdminQuizDetailSerializer,
    AdminQuizReviewActionSerializer,
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

        if not quiz.is_editable:
            raise ValidationError(
                "This quiz can no longer be edited (it has been submitted, "
                "approved, or published)."
            )

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


class BulkAddQuestionsView(APIView):
    """
    POST /teacher/quizzes/:pk/questions/bulk/

    Accepts { "questions": [ {text, marks, explanation, topic, difficulty,
    choices:[{text, is_correct}]}, ... ] } and creates them all in one
    transaction. Used by the bulk-paste importer and the "add from bank"
    drawer in the quiz builder.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

        if not quiz.is_editable:
            raise ValidationError(
                "This quiz can no longer be edited (it has been submitted, "
                "approved, or published)."
            )

        serializer = BulkQuestionCreateSerializer(
            data=request.data,
            context={"quiz": quiz},
        )
        serializer.is_valid(raise_exception=True)
        created = serializer.save()

        return Response(
            {"detail": f"{len(created)} question(s) added.", "count": len(created)},
            status=status.HTTP_201_CREATED,
        )


class SubmitQuizForReviewView(APIView):
    """
    PATCH /teacher/quizzes/:pk/publish/
    PATCH /teacher/quizzes/:pk/submit-for-review/  (same behaviour, clearer name)

    Historically this instantly published a quiz. Quizzes now go through
    admin verification first: this moves a DRAFT/REJECTED quiz to PENDING
    review. It only becomes visible to students (is_published=True) once an
    admin approves it — see AdminQuizReviewView.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not request.user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized.")

        if not quiz.is_editable:
            if quiz.review_status == Quiz.REVIEW_PENDING:
                raise ValidationError("This quiz is already awaiting admin review.")
            raise ValidationError("This quiz has already been approved and published.")

        if not quiz.questions.exists():
            raise ValidationError("Cannot submit an empty quiz for review.")

        total_marks = quiz.questions.aggregate(
            total=models.Sum("marks")
        )["total"] or 0

        quiz.total_marks = total_marks
        quiz.review_status = Quiz.REVIEW_PENDING
        quiz.submitted_for_review_at = timezone.now()
        # A resubmission after rejection clears the previous note/reviewer.
        quiz.review_note = ""
        quiz.reviewed_by = None
        quiz.reviewed_at = None
        quiz.save(update_fields=[
            "total_marks", "review_status", "submitted_for_review_at",
            "review_note", "reviewed_by", "reviewed_at",
        ])

        return Response(
            {"detail": "Quiz submitted for admin review.", "review_status": quiz.review_status},
            status=status.HTTP_200_OK,
        )


# Backward-compatible alias — some older frontend builds may still call the
# original class name. Both point at the same submit-for-review behaviour.
PublishQuizView = SubmitQuizForReviewView


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

        from django.utils import timezone as _tz
        from enrollments.models import Subscription as _Sub

        quizzes = (
            Quiz.objects
            .filter(
                subject__course__subscriptions__user=user,
                subject__course__subscriptions__status=_Sub.STATUS_ACTIVE,
                subject__course__subscriptions__expires_at__gt=_tz.now(),
                is_published=True,
            )
            .distinct()
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

        from enrollments.services import has_active_subscription, lock_payload
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        if not has_active_subscription(user=request.user, course=quiz.subject.course, learner_profile=learner):
            return Response(
                lock_payload(user=request.user, course=quiz.subject.course, learner_profile=learner),
                status=402,
            )

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


class CheckAnswerView(APIView):
    """
    POST /quizzes/:pk/questions/:qid/check/

    Practice-mode instant feedback. Records the answer against the
    student's active attempt (so a refresh doesn't lose progress and time
    tracking still feeds the results analytics) and immediately returns
    correctness + the explanation — something the public quiz serializer
    deliberately withholds for mock-style tests.

    Only usable on PRACTICE-type quizzes; mock exams must never leak the
    correct answer before the full paper is submitted.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk, qid):
        quiz = get_object_or_404(Quiz, pk=pk, is_published=True)

        if quiz.quiz_type != Quiz.TYPE_PRACTICE:
            raise PermissionDenied(
                "Instant feedback is only available in practice-mode quizzes."
            )

        question = get_object_or_404(
            Question.objects.prefetch_related("choices"), pk=qid, quiz=quiz
        )

        choice_id = request.data.get("selected_choice")
        choice = get_object_or_404(Choice, pk=choice_id, question=question)

        attempt = QuizAttempt.objects.filter(
            quiz=quiz, student=request.user, status=QuizAttempt.STATUS_PENDING,
        ).order_by("-attempt_number").first()
        if not attempt:
            raise ValidationError("Start the quiz first.")

        time_spent = request.data.get("time_spent", 0)
        try:
            time_spent = max(0, int(time_spent))
        except (TypeError, ValueError):
            time_spent = 0

        StudentAnswer.objects.update_or_create(
            attempt=attempt, question=question,
            defaults={
                "selected_choice": choice,
                "is_correct": choice.is_correct,
                "time_spent_seconds": time_spent,
            },
        )

        correct_choice = next(
            (c for c in question.choices.all() if c.is_correct), None
        )

        return Response({
            "is_correct": choice.is_correct,
            "correct_choice": {
                "id": correct_choice.id if correct_choice else None,
                "text": correct_choice.text if correct_choice else "",
            },
            "explanation": question.explanation,
        })


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
        else:
            from enrollments.services import has_active_subscription, lock_payload
            from accounts.auth_flow import get_active_profile

            learner = get_active_profile(request)
            if learner is None:
                return Response(
                    {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                    status=403,
                )
            if not has_active_subscription(user=request.user, course=quiz.subject.course, learner_profile=learner):
                return Response(
                    lock_payload(user=request.user, course=quiz.subject.course, learner_profile=learner),
                    status=402,
                )

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
        topic_stats = {}      # topic -> [correct, total]
        difficulty_stats = {}  # difficulty -> [correct, total]
        wrong_question_ids = []

        for answer in attempt.answers.all():
            q = answer.question
            correct_choice = next(
                (c for c in q.choices.all() if c.is_correct),
                None
            )
            result_questions.append({
                "id": q.id,
                "text": q.text,
                "selected_choice": answer.selected_choice.text,
                "correct_choice": correct_choice.text if correct_choice else "",
                "is_correct": answer.is_correct,
                "explanation": q.explanation,
                "topic": q.topic,
                "difficulty": q.difficulty,
                "time_spent_seconds": answer.time_spent_seconds,
                "marked_for_review": answer.marked_for_review,
            })

            if not answer.is_correct:
                wrong_question_ids.append(q.id)

            topic_key = q.topic or "General"
            t = topic_stats.setdefault(topic_key, [0, 0])
            t[1] += 1
            if answer.is_correct:
                t[0] += 1

            d = difficulty_stats.setdefault(q.difficulty, [0, 0])
            d[1] += 1
            if answer.is_correct:
                d[0] += 1

        topic_breakdown = [
            {"topic": k, "correct": v[0], "total": v[1],
             "pct": round(v[0] * 100.0 / v[1], 1) if v[1] else 0}
            for k, v in topic_stats.items()
        ]
        difficulty_order = {"easy": 0, "medium": 1, "hard": 2}
        difficulty_breakdown = sorted(
            (
                {"difficulty": k, "correct": v[0], "total": v[1],
                 "pct": round(v[0] * 100.0 / v[1], 1) if v[1] else 0}
                for k, v in difficulty_stats.items()
            ),
            key=lambda row: difficulty_order.get(row["difficulty"], 9),
        )

        # ── class average + percentile across all submitted attempts ──────
        all_scores = list(
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .values_list("score", flat=True)
        )
        class_avg_percent = 0.0
        percentile = 0.0
        if quiz.total_marks and all_scores:
            pct_scores = [s * 100.0 / quiz.total_marks for s in all_scores]
            class_avg_percent = round(sum(pct_scores) / len(pct_scores), 1)
            better_count = sum(1 for s in all_scores if s > attempt.score)
            percentile = round(better_count * 100.0 / len(all_scores), 1)

        # ── score trend: this student's last 8 submitted attempts in the
        # same subject (across quizzes), plus the class average for each ──
        recent_attempts = list(
            QuizAttempt.objects
            .filter(
                student=request.user,
                status=QuizAttempt.STATUS_SUBMITTED,
                quiz__subject=quiz.subject,
            )
            .select_related("quiz")
            .order_by("-submitted_at")[:8]
        )
        recent_attempts.reverse()

        score_trend = []
        for a in recent_attempts:
            if not a.quiz.total_marks:
                continue
            pct = round(a.score * 100.0 / a.quiz.total_marks, 1)
            sibling_scores = list(
                QuizAttempt.objects
                .filter(quiz=a.quiz, status=QuizAttempt.STATUS_SUBMITTED)
                .values_list("score", flat=True)
            )
            class_avg = (
                round(sum(sibling_scores) * 100.0 /
                      (len(sibling_scores) * a.quiz.total_marks), 1)
                if sibling_scores else 0
            )
            score_trend.append({
                "quiz_id": a.quiz.id,
                "quiz_title": a.quiz.title,
                "submitted_at": a.submitted_at,
                "pct": pct,
                "class_avg_pct": class_avg,
            })

        data = {
            "quiz_id": quiz.id,
            "title": quiz.title,
            "subject_name": quiz.subject.name,
            "teacher_name": quiz.created_by.email,
            "quiz_type": quiz.quiz_type,
            "total_marks": quiz.total_marks,
            "score": attempt.score,
            "submitted_at": attempt.submitted_at,
            "attempt_number": attempt.attempt_number,
            "questions": result_questions,
            "class_avg_percent": class_avg_percent,
            "percentile": percentile,
            "topic_breakdown": topic_breakdown,
            "difficulty_breakdown": difficulty_breakdown,
            "score_trend": score_trend,
            "wrong_question_ids": wrong_question_ids,
        }

        serializer = QuizResultSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        return Response(serializer.data)


class StudentQuizSubjectsView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        # FIX: query all enrolled subjects directly instead of filtering
        # through quizzes — so subjects without quizzes also appear
        from django.utils import timezone as _tz
        from enrollments.models import Subscription as _Sub

        subjects = (
            Subject.objects
            .filter(
                course__subscriptions__user=request.user,
                course__subscriptions__status=_Sub.STATUS_ACTIVE,
                course__subscriptions__expires_at__gt=_tz.now(),
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
            .select_related("student")
            .order_by("attempt_number")
        )

        data = [
            {
                "id": a.id,
                "attempt_number": a.attempt_number,
                "student_name": (
                    (lambda p: p.full_name if p and p.full_name else a.student.username)(
                        a.student.default_learner_profile()
                    )
                ),
                "submitted_at": a.submitted_at,
                "score": a.score,
                "total_marks": quiz.total_marks,
                "time_taken": None,
            }
            for a in attempts
        ]

        return Response({"title": quiz.title, "quiz_type": quiz.quiz_type, "attempts": data})


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

        student_summaries = list(
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .values("student_id", "student__email")
            .annotate(
                latest_submitted_at=Max("submitted_at"),
                best_score=Max("score"),
                average_score=Avg("score"),
                attempts_count=Count("id"),
            )
            .order_by("student__email")
        )

        # Resolve display names from learner profiles in one query (the legacy
        # one-to-one Profile model was removed; default profile preferred).
        from accounts.models import LearnerProfile
        _ids = [s["student_id"] for s in student_summaries]
        _name_map = {}
        for _lp in (
            LearnerProfile.objects
            .filter(account_id__in=_ids, is_active=True)
            .order_by("account_id", "-is_default", "created_at")
        ):
            _name_map.setdefault(
                _lp.account_id, (_lp.full_name or "").strip() or _lp.display_name
            )

        data = [
            {
                "student_id": s["student_id"],
                "student_name": _name_map.get(s["student_id"]) or s["student__email"],
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
            .select_related("student")
            .order_by("attempt_number")
        )


class TeacherQuizAttemptDetailView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        attempt = get_object_or_404(
            QuizAttempt.objects
            .select_related("student", "quiz")
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
            "student_name": (lambda p: p.full_name if p else attempt.student.username)(attempt.student.default_learner_profile()),
            "score": attempt.score,
            "total": attempt.quiz.total_marks,
            "submitted_at": attempt.submitted_at,
            "attempt_number": attempt.attempt_number,
            "questions": result_questions,
        })


# =====================================================
# TEACHER QUESTION BANK
# =====================================================

class TeacherQuestionBankView(generics.ListAPIView):
    """
    GET /teacher/question-bank/?scope=mine|school&subject=<id>&topic=&difficulty=&search=

    The bank is a filtered view over questions belonging to *finalized*
    (admin-approved) quizzes only, so what a teacher reuses has already
    been vetted:
      - scope=mine   → questions from the requesting teacher's own quizzes.
      - scope=school → questions from other teachers assigned to the same
                       subject(s) as the requester (their "school library").
    """
    serializer_class = BankQuestionSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get_queryset(self):
        user = self.request.user
        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        scope = self.request.query_params.get("scope", "mine")
        subject_id = self.request.query_params.get("subject")
        topic = self.request.query_params.get("topic", "").strip()
        difficulty = self.request.query_params.get("difficulty", "").strip()
        search = self.request.query_params.get("search", "").strip()

        assigned_subject_ids = SubjectTeacher.objects.filter(
            teacher=user
        ).values_list("subject_id", flat=True)

        qs = (
            Question.objects
            .filter(quiz__review_status=Quiz.REVIEW_APPROVED)
            .select_related("quiz", "quiz__subject", "quiz__created_by")
            .prefetch_related("choices")
        )

        if scope == "school":
            qs = qs.filter(
                quiz__subject_id__in=assigned_subject_ids
            ).exclude(quiz__created_by=user)
        else:
            qs = qs.filter(quiz__created_by=user)

        if subject_id:
            qs = qs.filter(quiz__subject_id=subject_id)
        else:
            # Never leak questions from subjects the teacher isn't assigned to.
            qs = qs.filter(quiz__subject_id__in=assigned_subject_ids)

        if topic:
            qs = qs.filter(topic__icontains=topic)
        if difficulty:
            qs = qs.filter(difficulty=difficulty)
        if search:
            qs = qs.filter(Q(text__icontains=search) | Q(topic__icontains=search))

        return qs.order_by("-created_at")


class TeacherBankFiltersView(APIView):
    """
    GET /teacher/question-bank/filters/

    Distinct topics + subjects available to the requesting teacher, for
    populating the bank's filter dropdowns (independent of pagination).
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        user = request.user
        if not user.has_role("TEACHER"):
            raise PermissionDenied("Only teachers allowed.")

        assigned_subject_ids = SubjectTeacher.objects.filter(
            teacher=user
        ).values_list("subject_id", flat=True)

        topics = (
            Question.objects
            .filter(
                quiz__review_status=Quiz.REVIEW_APPROVED,
                quiz__subject_id__in=assigned_subject_ids,
            )
            .exclude(topic="")
            .values_list("topic", flat=True)
            .distinct()
            .order_by("topic")
        )

        subjects = (
            Subject.objects
            .filter(id__in=assigned_subject_ids)
            .values("id", "name")
            .order_by("name")
        )

        return Response({
            "topics": list(topics),
            "subjects": list(subjects),
            "difficulties": [c[0] for c in Question.DIFFICULTY_CHOICES],
        })


# =====================================================
# ADMIN — academy quiz verification
# =====================================================

class AdminQuizListView(generics.ListAPIView):
    """
    GET /quizzes/admin/?status=pending|approved|rejected|draft&subject=<id>&search=

    Lists every quiz on the platform (the admin panel's "Academy Quizzes"
    section) so admins can find and verify submissions regardless of which
    teacher or subject they belong to.
    """
    serializer_class = AdminQuizListSerializer
    permission_classes = [IsAuthenticated, IsAdmin]

    def get_queryset(self):
        status_filter = self.request.query_params.get("status", "").strip()
        subject_id = self.request.query_params.get("subject", "").strip()
        search = self.request.query_params.get("search", "").strip()

        qs = (
            Quiz.objects
            .select_related("subject", "subject__course", "created_by")
            .annotate(
                questions_count=Count("questions", distinct=True),
                attempts_count=Count("attempts", distinct=True),
            )
        )

        if status_filter:
            qs = qs.filter(review_status=status_filter)
        if subject_id:
            qs = qs.filter(subject_id=subject_id)
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(created_by__email__icontains=search)
                | Q(subject__name__icontains=search)
            )

        return qs.order_by(
            models.Case(
                models.When(review_status=Quiz.REVIEW_PENDING, then=0),
                models.When(review_status=Quiz.REVIEW_DRAFT, then=2),
                default=1,
                output_field=models.IntegerField(),
            ),
            "-created_at",
        )


class AdminQuizDetailView(APIView):
    """GET /quizzes/admin/:pk/ — full quiz + questions for admin review."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects
            .select_related("subject", "subject__course", "created_by", "reviewed_by")
            .prefetch_related("questions__choices"),
            pk=pk,
        )
        return Response(AdminQuizDetailSerializer(quiz).data)


class AdminQuizReviewView(APIView):
    """
    POST /quizzes/admin/:pk/review/   { action: "approve"|"reject", reason }

    Approving publishes the quiz to students immediately. Rejecting sends
    it back to the teacher (with the reason) as an editable draft.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        quiz = get_object_or_404(Quiz, pk=pk)

        serializer = AdminQuizReviewActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        reason = serializer.validated_data.get("reason", "")

        if quiz.review_status != Quiz.REVIEW_PENDING:
            raise ValidationError(
                "Only quizzes awaiting review can be approved or rejected."
            )

        quiz.reviewed_by = request.user
        quiz.reviewed_at = timezone.now()

        if action == AdminQuizReviewActionSerializer.ACTION_APPROVE:
            quiz.review_status = Quiz.REVIEW_APPROVED
            quiz.review_note = reason
            quiz.is_published = True
        else:
            quiz.review_status = Quiz.REVIEW_REJECTED
            quiz.review_note = reason
            quiz.is_published = False

        quiz.save(update_fields=[
            "review_status", "review_note", "is_published",
            "reviewed_by", "reviewed_at",
        ])

        return Response(AdminQuizDetailSerializer(quiz).data)
