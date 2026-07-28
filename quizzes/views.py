import json
import logging

import requests
from django.conf import settings
from django.db import transaction
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
from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)

from accounts.permissions import IsEmailVerified, IsAdmin, require_teacher_context, IsTeacherContext
from accounts.auth_flow import get_active_profile
from enrollments.models import Enrollment
from django.db import models
from django.db.models import (
    Count, Avg, Max, Min, Q, Case, When, Value, F,
    FloatField, IntegerField, OuterRef, Subquery,
)
from django.db.models.functions import Coalesce

from courses.models import Subject, SubjectTeacher
from courses.services import teaches_subject

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


def _attempt_learner_name(attempt):
    """Display name for the learner who took an attempt.

    Prefers the attempt's OWN learner_profile (so a teacher sees which
    child on a shared account took it); falls back to the account's
    default profile for legacy rows, then to the username.
    """
    lp = attempt.learner_profile or attempt.student.default_learner_profile()
    if lp:
        name = (lp.full_name or "").strip() or (lp.display_name or "").strip()
        if name:
            return name
    return attempt.student.username or attempt.student.email


# =====================================================
# TEACHER VIEWS
# =====================================================

class CreateQuizView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        require_teacher_context(request)

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


class TeacherUpdateQuizView(APIView):
    """
    PATCH /teacher/quizzes/:pk/

    Edit a draft/rejected quiz's meta (title, quiz_type, time_limit_minutes)
    from the builder. Same owner + is_editable gate as every other
    quiz-mutation view in this file; questions are edited via
    BulkAddQuestionsView.put(), not here.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

        if not quiz.is_editable:
            raise ValidationError(
                "This quiz can no longer be edited (it has been submitted, "
                "approved, or published)."
            )

        serializer = QuizCreateSerializer(
            quiz,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        # subject is immutable once created — never allow it to move quizzes
        # between subjects via this endpoint.
        serializer.validated_data.pop("subject", None)
        serializer.save()

        return Response(
            {"id": quiz.id, "detail": "Quiz updated successfully."},
            status=status.HTTP_200_OK,
        )


class AddQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        require_teacher_context(request)

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
        require_teacher_context(request)

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

    def put(self, request, pk):
        """
        PUT /teacher/quizzes/:pk/questions/bulk/

        Replace-semantics sibling of the POST above, for the builder's
        "save": { "questions": [{id?, text, marks, order, explanation,
        topic, difficulty, source, choices:[{text,is_correct}]}, ...] }.
        Questions with an `id` already on this quiz are updated in place
        (choices fully replaced); questions without `id` are created;
        existing questions whose `id` is absent from the payload are
        deleted. All-or-nothing in one transaction.
        """
        require_teacher_context(request)

        quiz = get_object_or_404(Quiz, pk=pk)

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

        if not quiz.is_editable:
            raise ValidationError(
                "This quiz can no longer be edited (it has been submitted, "
                "approved, or published)."
            )

        payload = request.data.get("questions", [])
        if not isinstance(payload, list):
            raise ValidationError("`questions` must be a list.")

        existing_by_id = {str(q.id): q for q in quiz.questions.all()}
        valid_sources = {c[0] for c in Question.SOURCE_CHOICES}
        keep_ids = set()
        cleaned = []

        for i, q_data in enumerate(payload):
            choices = q_data.get("choices", [])
            if len(choices) < 2:
                raise ValidationError(f"Question {i + 1}: at least two choices required.")
            if sum(1 for c in choices if c.get("is_correct")) != 1:
                raise ValidationError(f"Question {i + 1}: exactly one correct answer required.")
            if not q_data.get("text", "").strip():
                raise ValidationError(f"Question {i + 1}: text is required.")
            if not q_data.get("explanation", "").strip():
                raise ValidationError(f"Question {i + 1}: explanation is required.")

            q_id = str(q_data["id"]) if q_data.get("id") else None
            if q_id and q_id not in existing_by_id:
                raise ValidationError(f"Question {i + 1}: not part of this quiz.")

            cleaned.append({
                "id": q_id,
                "text": q_data["text"].strip(),
                "marks": int(q_data.get("marks") or 1),
                "order": q_data.get("order", i),
                "explanation": q_data.get("explanation", "").strip(),
                "topic": q_data.get("topic", "") or "",
                "difficulty": q_data.get("difficulty") or Question.DIFFICULTY_MEDIUM,
                "source": q_data.get("source") if q_data.get("source") in valid_sources else Question.SOURCE_MANUAL,
                "choices": [
                    {"text": c.get("text", "").strip(), "is_correct": bool(c.get("is_correct"))}
                    for c in choices if c.get("text", "").strip()
                ],
            })
            if q_id:
                keep_ids.add(q_id)

        with transaction.atomic():
            quiz.questions.exclude(id__in=keep_ids).delete()

            for q_data in cleaned:
                choices_data = q_data.pop("choices")
                q_id = q_data.pop("id")
                if q_id:
                    Question.objects.filter(id=q_id).update(**q_data)
                    question = existing_by_id[q_id]
                    question.choices.all().delete()
                else:
                    question = Question.objects.create(quiz=quiz, **q_data)
                Choice.objects.bulk_create([
                    Choice(question_id=question.id, **c)
                    for c in choices_data
                ])

        return Response(
            {"detail": f"{len(cleaned)} question(s) saved.", "count": len(cleaned)},
            status=status.HTTP_200_OK,
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
        require_teacher_context(request)

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

        require_teacher_context(request)

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


class TeacherQuizDuplicateView(APIView):
    """
    POST /teacher/quizzes/:pk/duplicate/

    Deep-copies a quiz (any review_status) into a fresh, unpublished draft
    owned by the same teacher — questions and choices included, attempts
    and review history left behind.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(
            Quiz.objects.prefetch_related("questions__choices"), pk=pk
        )

        if quiz.created_by != request.user:
            raise PermissionDenied("Not authorized for this quiz.")

        new_quiz = Quiz.objects.create(
            subject=quiz.subject,
            batch=quiz.batch,
            created_by=request.user,
            title=f"{quiz.title} (copy)",
            description=quiz.description,
            time_limit_minutes=quiz.time_limit_minutes,
            quiz_type=quiz.quiz_type,
        )

        for q in quiz.questions.all():
            new_q = Question.objects.create(
                quiz=new_quiz,
                text=q.text,
                marks=q.marks,
                order=q.order,
                explanation=q.explanation,
                topic=q.topic,
                difficulty=q.difficulty,
                source=q.source,
            )
            Choice.objects.bulk_create([
                Choice(question=new_q, text=c.text, is_correct=c.is_correct)
                for c in q.choices.all()
            ])

        return Response(
            {"id": new_quiz.id, "detail": "Quiz duplicated as a new draft."},
            status=status.HTTP_201_CREATED,
        )


class TeacherAllQuizListView(generics.ListAPIView):
    """Every quiz across every subject this teacher is assigned to.

    The faculty Quizzes screen is one flat, subject-filtered list (design
    handoff screen 12). Before this existed the frontend called
    TeacherSubjectQuizListView once per subject and flattened client-side.

    NOTE the difference from the per-subject view: that one can compute the
    enrolled-student count ONCE as a scalar, because every quiz it returns
    belongs to the same course. Across subjects the denominator varies per row,
    so submission_rate needs a correlated subquery on each quiz's own course —
    reusing the scalar here would have divided every subject's attempts by one
    arbitrary course's enrolment.
    """

    serializer_class = TeacherQuizAnalyticsSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get_queryset(self):
        from enrollments.models import Enrollment

        enrolled_per_course = (
            Enrollment.objects
            .filter(course_id=OuterRef("subject__course_id"), status="ACTIVE")
            .values("course_id")
            .annotate(n=Count("id"))
            .values("n")[:1]
        )

        return (
            Quiz.objects
            .filter(subject__subject_teachers__teacher=self.request.user)
            .select_related("subject", "subject__course")
            .annotate(
                enrolled_count=Coalesce(
                    Subquery(enrolled_per_course, output_field=IntegerField()),
                    Value(0),
                ),
                total_attempts=Count("attempts", distinct=True),
                average_score=Avg("attempts__score"),
                highest_score=Max("attempts__score"),
                lowest_score=Min("attempts__score"),
                questions_count=Count("questions", distinct=True),
            )
            .annotate(
                submission_rate=Case(
                    When(
                        enrolled_count__gt=0,
                        then=Count("attempts", distinct=True) * 100.0 / F("enrolled_count"),
                    ),
                    default=Value(0.0),
                    output_field=FloatField(),
                )
            )
            # distinct(): a teacher listed twice on one subject would otherwise
            # duplicate every quiz on it.
            .distinct()
            .order_by("-created_at")
        )


class TeacherSubjectQuizListView(generics.ListAPIView):
    serializer_class = TeacherQuizAnalyticsSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        subject = get_object_or_404(
            Subject.objects.select_related("course"),
            id=subject_id
        )

        if not teaches_subject(user, subject):
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

        # Everything on this dashboard is per LEARNER PROFILE: which courses
        # are unlocked, which quizzes were attempted, and the scores shown.
        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        from django.utils import timezone as _tz
        from enrollments.models import Subscription as _Sub

        quizzes = (
            Quiz.objects
            .filter(
                subject__course__subscriptions__learner_profile=learner,
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
                        learner_profile=learner
                    ).order_by("-attempt_number"),
                    to_attr="user_attempts",
                ),
                Prefetch(
                    "attempts",
                    queryset=QuizAttempt.objects.filter(
                        learner_profile=learner,
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
                learner_profile=learner,
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


class StudentQuizStatsView(APIView):
    """
    GET /student/quizzes/stats/?subject=<id>

    Stat strip for the Hub, scoped to the active learner profile (and
    optionally one subject): practice streak, average mock score, questions
    solved this week, weakest topic. StudentAnswer has no timestamp of its
    own, so "this week"/streak use the parent attempt's started_at as a
    proxy for when the answering happened — fine for a rollup stat, not
    used anywhere scores are computed.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        from datetime import timedelta
        from django.db.models.functions import TruncDate

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        subject_id = request.query_params.get("subject")

        attempts_qs = QuizAttempt.objects.filter(learner_profile=learner)
        if subject_id:
            attempts_qs = attempts_qs.filter(quiz__subject_id=subject_id)

        # ── streak: consecutive days with activity, counting back from today
        # (or from yesterday if nothing has happened yet today) ──────────────
        activity_dates = set(
            attempts_qs.annotate(day=TruncDate("started_at")).values_list("day", flat=True)
        )
        today = timezone.localdate()
        cursor = today if today in activity_dates else today - timedelta(days=1)
        streak_days = 0
        while cursor in activity_dates:
            streak_days += 1
            cursor -= timedelta(days=1)

        # ── average mock score: best % per mock quiz, averaged ──────────────
        mock_attempts = (
            attempts_qs
            .filter(status=QuizAttempt.STATUS_SUBMITTED, quiz__quiz_type=Quiz.TYPE_MOCK)
            .select_related("quiz")
        )
        best_by_quiz = {}
        for a in mock_attempts:
            if not a.quiz.total_marks:
                continue
            pct = a.score * 100.0 / a.quiz.total_marks
            best_by_quiz[a.quiz_id] = max(pct, best_by_quiz.get(a.quiz_id, 0))
        avg_mock_score = round(sum(best_by_quiz.values()) / len(best_by_quiz), 1) if best_by_quiz else 0

        # ── questions solved this week ───────────────────────────────────────
        week_ago = timezone.now() - timedelta(days=7)
        all_answers = StudentAnswer.objects.filter(attempt__learner_profile=learner)
        if subject_id:
            all_answers = all_answers.filter(question__quiz__subject_id=subject_id)
        questions_solved = all_answers.filter(attempt__started_at__gte=week_ago).count()

        # ── weakest topic (same accumulation pattern as QuizResultView,
        # generalized across every attempt instead of one; topics with fewer
        # than 3 answers are skipped so a single lucky/unlucky guess can't
        # dominate the label) ────────────────────────────────────────────────
        topic_stats = {}
        for ans in all_answers.select_related("question"):
            topic_key = ans.question.topic or "General"
            t = topic_stats.setdefault(topic_key, [0, 0])
            t[1] += 1
            if ans.is_correct:
                t[0] += 1

        weakest_topic = None
        weakest_topic_accuracy = None
        worst_pct = None
        for topic_key, (correct, total) in topic_stats.items():
            if total < 3:
                continue
            pct = correct * 100.0 / total
            if worst_pct is None or pct < worst_pct:
                worst_pct = pct
                weakest_topic = topic_key
                weakest_topic_accuracy = round(pct, 1)

        return Response({
            "streak_days": streak_days,
            "avg_mock_score": avg_mock_score,
            "questions_solved": questions_solved,
            "weakest_topic": weakest_topic,
            "weakest_topic_accuracy": weakest_topic_accuracy,
        })


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

        # ── Reuse an existing PENDING attempt instead of creating a new one ──
        # Scoped to the ACTIVE LEARNER PROFILE: without this, a sibling on
        # the same account would resume (and submit into) another child's
        # in-flight attempt.
        existing_pending = QuizAttempt.objects.filter(
            quiz=quiz,
            learner_profile=learner,
            status=QuizAttempt.STATUS_PENDING,
        ).order_by("-attempt_number").first()

        if existing_pending:
            # Student refreshed the page or navigated back — resume the same attempt
            return Response(
                {"detail": "Resuming existing attempt.",
                    "attempt_id": existing_pending.id},
                status=status.HTTP_200_OK,
            )

        # Create a new attempt (first attempt or re-attempt after submitting).
        # Attempt numbering is per profile — each child counts from 1.
        last_attempt = QuizAttempt.objects.filter(
            quiz=quiz,
            learner_profile=learner
        ).order_by("-attempt_number").first()

        new_attempt_number = (
            last_attempt.attempt_number + 1) if last_attempt else 1

        new_attempt = QuizAttempt.objects.create(
            quiz=quiz,
            student=request.user,
            learner_profile=learner,
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

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        attempt = QuizAttempt.objects.filter(
            quiz=quiz, learner_profile=learner, status=QuizAttempt.STATUS_PENDING,
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
        require_teacher_context(request)

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

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        # Support ?attempt=<id> to view a specific attempt — the active
        # profile's own attempts only, so one child can't open a sibling's
        # results by guessing an attempt id.
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
                learner_profile=learner,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
        else:
            attempt = (
                QuizAttempt.objects
                .filter(
                    quiz=quiz,
                    learner_profile=learner,
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

        # ── score trend: this LEARNER PROFILE's last 8 submitted attempts in
        # the same subject (across quizzes), plus the class average for each ──
        recent_attempts = list(
            QuizAttempt.objects
            .filter(
                learner_profile=learner,
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

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        attempts = (
            QuizAttempt.objects
            .filter(
                quiz=quiz,
                learner_profile=learner,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
            .select_related("student", "learner_profile")
            .order_by("attempt_number")
        )

        data = [
            {
                "id": a.id,
                "attempt_number": a.attempt_number,
                "student_name": _attempt_learner_name(a),
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

        require_teacher_context(request)

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
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]
    serializer_class = TeacherQuizAttemptSerializer

    def get_queryset(self):
        user = self.request.user
        quiz_id = self.kwargs["quiz_id"]
        student_id = self.kwargs["student_id"]

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
            "student_name": _attempt_learner_name(attempt),
            "score": attempt.score,
            "total": attempt.quiz.total_marks,
            "submitted_at": attempt.submitted_at,
            "attempt_number": attempt.attempt_number,
            "questions": result_questions,
        })


def _attempted_profile_ids(attempts):
    """Learner-profile ids behind a list of attempts, falling back to the
    student's default profile for legacy attempts written before
    QuizAttempt.learner_profile existed (learner_profile is NULL there) —
    same fallback `_attempt_learner_name` above already relies on, needed
    here too so old attempts aren't invisible to attempted/not-attempted
    counting."""
    ids = set()
    for a in attempts:
        if a.learner_profile_id:
            ids.add(a.learner_profile_id)
        else:
            lp = a.student.default_learner_profile()
            if lp:
                ids.add(lp.id)
    return ids


class TeacherQuizAnalyticsView(APIView):
    """
    GET /teacher/quizzes/:pk/analytics/

    Item analysis (per-question % correct), score distribution, and the
    not-yet-attempted roster for one quiz — the aggregate companion to
    TeacherQuizAttemptsView's per-student roster.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject", "subject__course"), pk=pk
        )

        if not SubjectTeacher.objects.filter(
            subject=quiz.subject, teacher=request.user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        submitted_attempts = list(
            QuizAttempt.objects.filter(
                quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED,
            ).select_related("student").prefetch_related("answers")
        )

        items = []
        for q in quiz.questions.all().order_by("order"):
            answers = StudentAnswer.objects.filter(
                question=q, attempt__status=QuizAttempt.STATUS_SUBMITTED,
            )
            total = answers.count()
            correct = answers.filter(is_correct=True).count()
            items.append({
                "id": q.id,
                "order": q.order + 1,
                "text": q.text,
                "pct_correct": round(correct * 100.0 / total, 1) if total else 0,
            })

        enrolled_profile_ids = set(
            Enrollment.objects.filter(course=quiz.subject.course, status="ACTIVE")
            .exclude(learner_profile__isnull=True)
            .values_list("learner_profile_id", flat=True)
        )
        attempted_profile_ids = _attempted_profile_ids(submitted_attempts)
        total_students = len(enrolled_profile_ids)
        attempted_count = len(enrolled_profile_ids & attempted_profile_ids)

        pct_scores = []
        durations = []
        if quiz.total_marks:
            for a in submitted_attempts:
                pct_scores.append(a.score * 100.0 / quiz.total_marks)
        for a in submitted_attempts:
            durations.append(sum(ans.time_spent_seconds for ans in a.answers.all()))

        class_average = round(sum(pct_scores) / len(pct_scores), 1) if pct_scores else 0
        sorted_scores = sorted(pct_scores)
        n = len(sorted_scores)
        if n:
            mid = n // 2
            median = sorted_scores[mid] if n % 2 else (sorted_scores[mid - 1] + sorted_scores[mid]) / 2
            median = round(median, 1)
        else:
            median = 0
        avg_time_seconds = round(sum(durations) / len(durations)) if durations else None

        buckets = [("0-20", 0, 20), ("21-40", 21, 40), ("41-60", 41, 60), ("61-80", 61, 80), ("81-100", 81, 100)]
        score_distribution = [
            {"range": label, "count": sum(1 for p in pct_scores if lo <= p <= hi)}
            for label, lo, hi in buckets
        ]

        not_attempted_ids = enrolled_profile_ids - attempted_profile_ids
        not_attempted = []
        if not_attempted_ids:
            from accounts.models import LearnerProfile
            for lp in LearnerProfile.objects.filter(id__in=not_attempted_ids):
                name = (lp.full_name or "").strip() or (lp.display_name or "").strip() or "Student"
                not_attempted.append({"id": lp.id, "name": name})

        return Response({
            "title": quiz.title,
            "attempted_count": attempted_count,
            "total_students": total_students,
            "class_average": class_average,
            "median": median,
            "avg_time_seconds": avg_time_seconds,
            "time_limit_minutes": quiz.time_limit_minutes,
            "items": items,
            "score_distribution": score_distribution,
            "not_attempted": not_attempted,
        })


class TeacherQuizRemindView(APIView):
    """
    POST /teacher/quizzes/:pk/remind/

    Nudges every enrolled learner who hasn't submitted an attempt yet via
    the standard notification pipeline (see notifications/policy.py's
    "quiz.reminder" entry).
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject", "subject__course"), pk=pk
        )

        if not SubjectTeacher.objects.filter(
            subject=quiz.subject, teacher=request.user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        enrolled_profile_ids = set(
            Enrollment.objects.filter(course=quiz.subject.course, status="ACTIVE")
            .exclude(learner_profile__isnull=True)
            .values_list("learner_profile_id", flat=True)
        )
        submitted_attempts = list(
            QuizAttempt.objects.filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .select_related("student")
        )
        attempted_profile_ids = _attempted_profile_ids(submitted_attempts)
        not_attempted_ids = enrolled_profile_ids - attempted_profile_ids
        if not not_attempted_ids:
            return Response({"detail": "Everyone has already attempted this quiz.", "count": 0})

        from accounts.models import LearnerProfile
        from notifications.services import notify

        profiles = list(
            LearnerProfile.objects.filter(id__in=not_attempted_ids).select_related("account")
        )
        sent = 0
        for lp in profiles:
            if not lp.account:
                continue
            notify(
                recipient=lp.account,
                verb="quiz.reminder",
                title=f'Reminder: "{quiz.title}" is still pending',
                body=f"Your teacher sent a reminder to complete this {quiz.get_quiz_type_display().lower()}.",
                actor=request.user,
                link_url=f"/subjects/quiz/{quiz.subject_id}",
                learner_profile=lp,
            )
            sent += 1

        return Response({"detail": f"Reminder sent to {sent} student(s).", "count": sent})


class TeacherGenerateAIQuestionsView(APIView):
    """
    POST /teacher/quizzes/generate-ai   { topic, difficulty, count }

    Drafts MCQs via OpenAI and returns them to the client only — nothing is
    written to the DB here, so every AI-drafted question still goes through
    the normal builder-review + admin-approval path before it can reach a
    student. Requires OPENAI_API_KEY to be set in the environment.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quiz_ai_generate"

    def post(self, request):
        require_teacher_context(request)

        topic = (request.data.get("topic") or "").strip()
        if not topic:
            raise ValidationError("topic is required.")
        difficulty = (request.data.get("difficulty") or "Mixed").strip()
        try:
            count = max(1, min(10, int(request.data.get("count") or 3)))
        except (TypeError, ValueError):
            count = 3

        api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured; cannot generate questions."
            )

        schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "topic": {"type": "string"},
                            "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                            "marks": {"type": "integer"},
                            "explanation": {"type": "string"},
                            "choices": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "is_correct": {"type": "boolean"},
                                    },
                                    "required": ["text", "is_correct"],
                                    "additionalProperties": False,
                                },
                                "minItems": 4,
                                "maxItems": 4,
                            },
                        },
                        "required": ["text", "topic", "difficulty", "marks", "explanation", "choices"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        }

        prompt = (
            f'Write {count} multiple-choice quiz question(s) on the topic "{topic}" '
            f"at {difficulty} difficulty. Each question needs exactly 4 answer "
            f"choices with exactly one marked is_correct=true, a short explanation "
            f"of the correct answer, a 1-3 word topic tag, and marks=2. Keep "
            f"questions unambiguous and distractors plausible."
        )

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": "You are a precise quiz-question writer for a school exam platform."},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "quiz_questions", "schema": schema, "strict": True},
                    },
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            drafted = json.loads(content)["questions"]
        except Exception:
            logger.exception("Quiz AI generation failed")
            raise ValidationError("AI generation failed. Try again or add questions manually.")

        return Response({"questions": drafted})


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
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
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
        require_teacher_context(request)

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
