import json
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from rest_framework.exceptions import PermissionDenied
from rest_framework import generics
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)

from accounts.permissions import IsEmailVerified, IsAdmin, require_teacher_context, IsTeacherContext, _in_teacher_context
from accounts.auth_flow import get_active_profile
from enrollments.models import Enrollment
from enrollments.services import active_batch_id
from django.db import models
from django.db.models import (
    Count, Avg, Max, Min, Q, Case, When, Value, F,
    FloatField, IntegerField, OuterRef, Subquery,
)
from django.db.models.functions import Coalesce

from courses.board_display import board_name_via
from courses.models import Subject, TeachingAssignment
from courses.services import teaches_subject

from .models import (
    Quiz, QuizSection, QuizAttempt, Question, Choice, StudentAnswer,
)
from .visibility import batch_scope_q, learner_may_see_quiz
from .serializers import (
    QuizCreateSerializer,
    QuestionCreateSerializer,
    BulkQuestionCreateSerializer,
    QuizDashboardSerializer,
    QuizAssignSerializer,
    QuizSectionSerializer,
    QuizSubmitSerializer,
    QuizDetailSerializer,
    QuizDetailTeacherSerializer,
    QuizResultSerializer,
    TeacherQuizAnalyticsSerializer,
    TeacherQuizAttemptSerializer,
    BankQuestionSerializer,
    QuestionBankStateSerializer,
    AdminQuizListSerializer,
    AdminQuizDetailSerializer,
    AdminQuizReviewActionSerializer,
)


# Every teacher-facing "how did the class do" aggregate must count only
# SUBMITTED attempts. A PENDING row is a student who opened the quiz and
# closed the tab: it carries score=0 and no answers, so including it dragged
# `average_score` toward zero and inflated `total_attempts`. The analytics
# screen (TeacherQuizAnalyticsView) has always filtered on SUBMITTED, so the
# quiz card and its own analytics page disagreed on the same quiz — the card
# read "avg 6.8 · 4 attempts" where analytics read "90% over 3".
_SUBMITTED_ATTEMPTS = Q(attempts__status=QuizAttempt.STATUS_SUBMITTED)


def _assert_learner_may_see_quiz(learner, quiz):
    """Raises Http404 unless `quiz`'s batch scope includes this learner.

    StartQuizView, QuizDetailView and SubmitQuizView resolve a quiz by UUID
    with only `is_assigned` + subscription checks — same shape assignments'
    per-object endpoints had before _assert_learner_may_see_assignment. Now
    that QuizCreateSerializer actually sets Quiz.batch (see its model-field
    comment), a batch-scoped quiz needs the same isolation or a Batch-B
    student who has/guesses the UUID can start, view and submit a
    Batch-A-only quiz. Course-wide quizzes are unaffected.

    Delegates to quizzes/visibility.py so the per-object rule and the
    queryset rule can never drift; that module handles the M2M `batches`
    plus the legacy `batch` FK fallback.

    404 rather than 403, matching the assignments precedent: a quiz scoped
    to a class the learner isn't in shouldn't be confirmed to exist.
    """
    if not learner_may_see_quiz(learner, quiz):
        raise Http404


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
        topic, difficulty, source, section?, suggest_to_bank?,
        choices:[{text,is_correct}]}, ...] }.

        `section` and `suggest_to_bank` are applied ONLY when the key is
        present — an older client that omits them must not have its grouping
        stripped or its bank opt-in reset.
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
        section_ids = {str(s.id) for s in quiz.sections.all()}
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

            # `section` is applied ONLY when the key is present. Defaulting a
            # missing key to None would re-run the section-orphaning bug from
            # the other side: the pre-Phase-5 builder doesn't send `section`,
            # so every save would silently strip the grouping off a sectioned
            # mock paper. Send `"section": null` to deliberately ungroup one.
            section_given = "section" in q_data
            section_id = q_data.get("section")
            if section_given and section_id is not None:
                section_id = str(section_id)
                if section_id not in section_ids:
                    raise ValidationError(
                        f"Question {i + 1}: section is not a section of this quiz."
                    )

            # Same present-vs-absent rule as `section`, and for the same
            # reason: a client that doesn't send the key must not have its
            # questions silently re-flagged. Absent means "leave it alone".
            suggest_given = "suggest_to_bank" in q_data

            cleaned.append({
                "id": q_id,
                **({"section_id": section_id} if section_given else {}),
                **({"suggest_to_bank": bool(q_data.get("suggest_to_bank"))}
                   if suggest_given else {}),
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
                    # setattr + save(), NOT queryset.update(): .update() goes
                    # straight to SQL and never runs Question.save(), so the
                    # `suggest_to_bank=False ⟹ bank_state="private"` invariant
                    # was silently skipped on every builder save. Harmless
                    # while this endpoint ignored suggest_to_bank; the moment
                    # it accepts one (above), a teacher turning the switch off
                    # would have left bank_state="suggested" and the question
                    # sitting in the admin's curation queue anyway.
                    question = existing_by_id[q_id]
                    for field, value in q_data.items():
                        setattr(question, field, value)
                    question.save()
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


class TeacherQuizAssignView(APIView):
    """PATCH /teacher/quizzes/:pk/assign/   {assign: bool, batch_ids?: [...]}

    The Phase 1 endpoint: a teacher makes their OWN quiz live for their OWN
    batches, with no admin involvement. Independent of `review_status`, which
    is now purely informational — SubmitQuizForReviewView (publish/ and
    submit-for-review/) is untouched and still only asks an admin to look at
    the questions.

    Writes four things, in an order that matters:

      1. `batches` (M2M) — the real scope.
      2. `batch` (legacy FK) = batch_ids[0], or NULL. Old clients still read
         this, and quizzes/visibility.py still falls back to it.
      3. `is_assigned` — the gate every student queryset now reads.
      4. `is_published` — mirrored for back-compat only. Retires in Phase 10.

    The M2M is written BEFORE the save because activity/signals.py's
    `quiz_published` post_save receiver resolves the notify audience from the
    batch scope. Save first and it would notify the PREVIOUS batches.
    """

    permission_classes = [IsAuthenticated, IsEmailVerified]

    def patch(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"), pk=pk,
        )
        if quiz.created_by != request.user:
            raise PermissionDenied("You did not create this quiz.")

        serializer = QuizAssignSerializer(
            data=request.data, context={"quiz": quiz},
        )
        serializer.is_valid(raise_exception=True)
        assign = serializer.validated_data["assign"]
        # Absent (vs []) means "don't touch the scope" — see the serializer's
        # docstring for why that distinction is load-bearing.
        batches = serializer.validated_data.get("batch_ids")

        with transaction.atomic():
            if batches is not None:
                quiz.batches.set(batches)
                quiz.batch = batches[0] if batches else None

            quiz.is_assigned = assign
            quiz.is_published = assign
            quiz.save(update_fields=[
                "batch", "is_assigned", "is_published", "updated_at",
            ])

        return Response(
            QuizDetailTeacherSerializer(quiz, context={"request": request}).data
        )


class TeacherQuizSectionsView(APIView):
    """PUT /teacher/quizzes/:pk/sections/   {sections: [{id?, name, order?,
    instructions?}, ...]}

    Replaces the quiz's section set and returns the result. Owner-only, same
    gate as TeacherQuizAssignView (created_by == request.user, after
    require_teacher_context).

    ── REPLACE SEMANTICS, and why this is not "delete all, recreate" ───────
    The obvious implementation — `quiz.sections.all().delete()` then create
    everything from the payload — is silently destructive. Question.section
    is SET_NULL, so deleting a section NULLs the section FK of every question
    in it. A teacher who opened the builder, renamed "Section A" to
    "Section A · Objective" and hit save would get back a paper whose
    sections exist but are all empty: every question flattened into the
    unsectioned list, no error, nothing to undo. The rename is the single
    most common edit, so that bug would be near-universal.

    So sections are matched BY ID:

      · payload entry WITH an id that belongs to this quiz → updated in
        place. The row survives, so `question.section_id` still points at it
        and the grouping is untouched.
      · payload entry with NO id → created.
      · existing section whose id is absent from the payload → deleted, and
        SET_NULL merges its questions back into the flat list. That is the
        intended "ungroup this section" behaviour, and it is the ONLY case
        that should move any question.
      · id that isn't one of this quiz's sections → 400, not a silent
        create. It means the client is confused about which quiz it is
        editing, and inventing a section would hide that.

    Deliberately NOT gated on `quiz.is_editable` (unlike AddQuestionView):
    is_editable tracks the admin review workflow, which Phase 1 severed from
    the teacher's own control of their own quiz. Sections are structure, not
    question content, and a teacher must be able to fix a section title on a
    live paper without asking an admin.
    """

    permission_classes = [IsAuthenticated, IsEmailVerified]

    def put(self, request, pk):
        require_teacher_context(request)

        quiz = get_object_or_404(Quiz.objects.select_related("subject"), pk=pk)
        if quiz.created_by != request.user:
            raise PermissionDenied("You did not create this quiz.")

        payload = request.data.get("sections", [])
        if not isinstance(payload, list):
            raise ValidationError("`sections` must be a list.")

        existing = {str(s.id): s for s in quiz.sections.all()}
        seen_ids = set()
        cleaned = []

        for i, row in enumerate(payload):
            if not isinstance(row, dict):
                raise ValidationError(f"Section {i + 1}: expected an object.")
            name = (row.get("name") or "").strip()
            if not name:
                raise ValidationError(f"Section {i + 1}: name is required.")
            if len(name) > 80:
                raise ValidationError(
                    f"Section {i + 1}: name is longer than 80 characters."
                )

            sid = str(row["id"]) if row.get("id") else None
            if sid is not None:
                if sid not in existing:
                    raise ValidationError(
                        f"Section {i + 1}: not a section of this quiz."
                    )
                if sid in seen_ids:
                    raise ValidationError(
                        f"Section {i + 1}: id repeated in the payload."
                    )
                seen_ids.add(sid)

            # order defaults to the payload position, so a client that just
            # sends the list in the right order gets the right order.
            try:
                order = int(row["order"]) if row.get("order") is not None else i
            except (TypeError, ValueError):
                raise ValidationError(f"Section {i + 1}: order must be a number.")
            if order < 0:
                raise ValidationError(f"Section {i + 1}: order must not be negative.")

            cleaned.append({
                "id": sid,
                "name": name,
                "order": order,
                "instructions": (row.get("instructions") or "").strip(),
            })

        with transaction.atomic():
            # Only genuinely removed sections are deleted — see the docstring.
            quiz.sections.exclude(id__in=seen_ids).delete()

            for row in cleaned:
                sid = row.pop("id")
                if sid:
                    QuizSection.objects.filter(id=sid).update(**row)
                else:
                    QuizSection.objects.create(quiz=quiz, **row)

        return Response(
            QuizSectionSerializer(
                quiz.sections.all(), many=True, context={"request": request},
            ).data,
            status=status.HTTP_200_OK,
        )


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
            .filter(
                subject__teaching_assignments__teacher=self.request.user,
                subject__teaching_assignments__is_active=True,
            )
            .select_related("subject", "subject__course__board")
            .annotate(
                enrolled_count=Coalesce(
                    Subquery(enrolled_per_course, output_field=IntegerField()),
                    Value(0),
                ),
                total_attempts=Count("attempts", filter=_SUBMITTED_ATTEMPTS, distinct=True),
                average_score=Avg("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                highest_score=Max("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                lowest_score=Min("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                questions_count=Count("questions", distinct=True),
                # DISTINCT LEARNERS, not attempts. submission_rate used to
                # divide the raw attempt count by the enrolment, so one
                # student retaking 15 times in a class of 10 read "150%
                # submitted". Counting learner_profile skips legacy
                # (pre-profile) attempts where it is NULL — those under-count
                # rather than over-count, which is the safe direction here.
                submitted_learners=Count(
                    "attempts__learner_profile",
                    filter=_SUBMITTED_ATTEMPTS,
                    distinct=True,
                ),
            )
            .annotate(
                submission_rate=Case(
                    When(
                        enrolled_count__gt=0,
                        then=F("submitted_learners") * 100.0 / F("enrolled_count"),
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
            .select_related("subject", "subject__course__board")
            # Same SUBMITTED-only / distinct-learner rules as
            # TeacherAllQuizListView above — the two endpoints feed the same
            # card, so they must not disagree.
            .annotate(
                total_attempts=Count("attempts", filter=_SUBMITTED_ATTEMPTS, distinct=True),
                average_score=Avg("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                highest_score=Max("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                lowest_score=Min("attempts__score", filter=_SUBMITTED_ATTEMPTS),
                questions_count=Count("questions", distinct=True),
                submission_rate=Count(
                    "attempts__learner_profile",
                    filter=_SUBMITTED_ATTEMPTS,
                    distinct=True,
                ) * 100.0 / (enrolled_count_subquery or 1),
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
        course_id = request.query_params.get("course")

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
                is_assigned=True,
            )
            .distinct()
            .select_related("subject", "subject__course__board", "created_by")
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
                    # Only attempts with at least one answer count as a real
                    # completion. A 0-answer SUBMITTED row (auto-closed on
                    # timer expiry, or an empty grace-window auto-submit)
                    # must NOT flip the quiz to "completed" — that is exactly
                    # what locked students out of quizzes they never answered.
                    queryset=QuizAttempt.objects.filter(
                        learner_profile=learner,
                        status=QuizAttempt.STATUS_SUBMITTED,
                    ).annotate(_answer_count=Count("answers"))
                    .filter(_answer_count__gt=0)
                    .order_by("-attempt_number"),
                    to_attr="user_submitted_attempts",
                ),
            )
            .distinct()
        )

        # Course scoping. A learner profile can hold live subscriptions to
        # several courses at once (Class 7 + Class 10 + Class 12), and the
        # subscription join above spans ALL of them — so without this the
        # Hub showed another class's quizzes under the active course, which
        # is exactly what was reported. Assignments/materials avoid this by
        # taking the course id as a URL kwarg; quizzes is a flat endpoint,
        # so it takes it as a query param instead. Optional for backwards
        # compatibility with any caller that genuinely wants the whole
        # profile (e.g. a future cross-course "everything" view).
        if course_id:
            quizzes = quizzes.filter(subject__course_id=course_id)

            # Batch isolation, finally enforced — see the warning on
            # Quiz.batch. Course-wide quizzes plus this learner's own
            # batch's quizzes, matching materials/views.py's
            # StudentCourseMaterials exactly. Only meaningful when a course
            # is in scope, since a batch belongs to one course.
            #
            # batch_scope_q covers the M2M `batches` AND the legacy `batch`
            # FK in one rule, via Exists() subqueries — so it adds no join
            # and cannot duplicate rows into the questions_count Count()
            # annotated above. See quizzes/visibility.py.
            batch_id = active_batch_id(
                learner_profile=learner,
                course_id=course_id,
            )
            quizzes = quizzes.filter(batch_scope_q(batch_id))

        if subject_id:
            quizzes = quizzes.filter(subject_id=subject_id)

        # Mirrors the prefetch above: a quiz only counts as "completed" once
        # the profile has a SUBMITTED attempt that actually holds answers, so
        # a 0-answer ghost row can never mark it done / send the student to
        # review.
        submitted_ids = set(
            QuizAttempt.objects.filter(
                learner_profile=learner,
                status=QuizAttempt.STATUS_SUBMITTED,
            ).annotate(_answer_count=Count("answers"))
            .filter(_answer_count__gt=0)
            .values_list("quiz_id", flat=True).distinct()
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
    GET /student/quizzes/stats/?course=<id>&subject=<id>

    Stat strip for the Hub, scoped to the active learner profile (and
    optionally one course and/or subject): practice streak, average mock
    score, questions solved this week, weakest topic. Without ?course= the
    numbers are profile-wide across every subscribed course, which made the
    strip read identically no matter which class the learner had selected —
    the Hub always sends it. StudentAnswer has no timestamp of its
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
        course_id = request.query_params.get("course")

        attempts_qs = QuizAttempt.objects.filter(learner_profile=learner)
        if course_id:
            attempts_qs = attempts_qs.filter(quiz__subject__course_id=course_id)
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
            # Clamped on the upper end for the same reason as
            # QuizDashboardSerializer.get_best_score — total_marks can fall
            # out of sync with a quiz's questions. NOT clamped on the lower
            # end: negative marking means pct can legitimately be negative,
            # and defaulting the "no entry yet" case to 0 (instead of -inf)
            # used to silently floor a quiz whose every attempt was negative
            # at 0 — max(pct, 0) always won against a first, negative pct.
            pct = min(100.0, a.score * 100.0 / a.quiz.total_marks)
            best_by_quiz[a.quiz_id] = max(
                pct, best_by_quiz.get(a.quiz_id, float("-inf"))
            )
        avg_mock_score = round(sum(best_by_quiz.values()) / len(best_by_quiz), 1) if best_by_quiz else 0

        # ── questions solved this week ───────────────────────────────────────
        week_ago = timezone.now() - timedelta(days=7)
        # Course-scoped for the same reason attempts_qs is: this queryset
        # feeds BOTH questions_solved and weakest_topic, so leaving it
        # profile-wide keeps two of the four stat cards reading identically
        # no matter which class is selected.
        all_answers = StudentAnswer.objects.filter(attempt__learner_profile=learner)
        if course_id:
            all_answers = all_answers.filter(question__quiz__subject__course_id=course_id)
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

    ⚠️ Starting a fresh attempt over an ALREADY-SUBMITTED one now requires
    an explicit `{"new_attempt": true}` in the body. Without it, this
    returns the submitted attempt with `already_submitted: true` and creates
    nothing. Reason: the attempt route re-mounts and calls this endpoint on
    every mount, and the browser Back button lands there — so a single stray
    back-click from the result screen silently burned an attempt. That is not
    cosmetic: it inflates attempt_number past `reveal_answers_after`, which
    permanently costs the learner access to the answer key, and it reshuffles
    the question order under a new attempt key. Unlimited retakes remain
    intentional; they just have to be ASKED for (the Reattempt buttons pass
    the flag) rather than happening by accident.

    Phase 4 adds a separate, second condition: `Quiz.max_attempts` (NULL =
    unlimited, 1 = single-attempt mock). It is a QUOTA, not another copy of
    the flag above — see the inline comment at the check for how the two
    compose and why the quota is evaluated first.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject__course__board"),
            pk=pk,
            is_assigned=True,
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
        _assert_learner_may_see_quiz(learner, quiz)

        # Lock the learner profile for the whole check-then-create critical
        # section below. Without this, a double-click or duplicate tab fires
        # two concurrent requests that both see "no existing PENDING attempt",
        # both compute the same next attempt_number, and the second INSERT
        # dies on uniq_attempt_per_profile_number — an unhandled
        # IntegrityError → 500, instead of gracefully resuming the attempt
        # the first request already created. Same pattern as the already-
        # fixed capacity-limited-booking races (skills/views.py's
        # ExpertProfile lock, enrollments/payment_views.py's Batch lock).
        from accounts.models import LearnerProfile
        with transaction.atomic():
            LearnerProfile.objects.select_for_update().get(pk=learner.pk)

            # ── Reuse an existing PENDING attempt instead of creating a new
            # one ── Scoped to the ACTIVE LEARNER PROFILE: without this, a
            # sibling on the same account would resume (and submit into)
            # another child's in-flight attempt.
            existing_pending = QuizAttempt.objects.filter(
                quiz=quiz,
                learner_profile=learner,
                status=QuizAttempt.STATUS_PENDING,
            ).order_by("-attempt_number").first()

            if existing_pending:
                expired = False
                if quiz.time_limit_minutes:
                    from .serializers import SUBMIT_GRACE_SECONDS
                    deadline = existing_pending.started_at + timedelta(
                        minutes=quiz.time_limit_minutes, seconds=SUBMIT_GRACE_SECONDS,
                    )
                    expired = timezone.now() > deadline
                if not expired:
                    # Student refreshed the page or navigated back — resume the same attempt
                    return Response(
                        {
                            "detail": "Resuming existing attempt.",
                            "attempt_id": existing_pending.id,
                            "started_at": existing_pending.started_at,
                            "expires_at": (
                                existing_pending.started_at
                                + timedelta(minutes=quiz.time_limit_minutes)
                                if quiz.time_limit_minutes else None
                            ),
                        },
                        status=status.HTTP_200_OK,
                    )
                # Missed the deadline without ever calling submit (tab closed,
                # browser crashed, or the auto-submit request itself failed).
                # DISCARD the abandoned attempt rather than resuming it forever
                # (which would permanently block a fresh attempt) OR flipping it
                # to SUBMITTED (what this used to do). A 0-answer "submitted" row
                # is poison: get_status / submitted_ids treat every SUBMITTED
                # attempt as a real completion, so the dashboard reports the quiz
                # as *completed* and QuizHub routes the student to the review
                # screen — locking them out of a quiz they never actually
                # answered. Deleting is safe: a PENDING attempt past its deadline
                # has no answers to preserve (StudentAnswer rows are written by
                # SubmitQuizView.save(), which flips the attempt to SUBMITTED, or
                # by practice-mode CheckAnswerView — and practice quizzes are
                # untimed, so this expiry path never runs for them). It also
                # restores fair attempt numbering: the student's first real
                # attempt is #1 again and gets its answer-key reveal, instead of
                # having #1 silently burned by a ghost. Guarded on the answer
                # count anyway, so a resumed attempt that somehow holds answers is
                # closed out (SUBMITTED), never destroyed.
                if existing_pending.answers.exists():
                    existing_pending.status = QuizAttempt.STATUS_SUBMITTED
                    existing_pending.submitted_at = timezone.now()
                    existing_pending.save(update_fields=["status", "submitted_at"])
                else:
                    existing_pending.delete()

            # Create a new attempt (first attempt or re-attempt after submitting).
            # Attempt numbering is per profile — each child counts from 1.
            last_attempt = QuizAttempt.objects.filter(
                quiz=quiz,
                learner_profile=learner
            ).order_by("-attempt_number").first()

            # ── Attempt QUOTA (Quiz.max_attempts, Phase 4) ────────────────
            # Checked BEFORE the `new_attempt` intent gate below, because the
            # two are different questions and the quota is the harder no:
            #
            #   max_attempts  = "how many attempts is this learner ENTITLED
            #                    to" — a mock-test rule, NULL = unlimited.
            #   new_attempt   = "did the learner ASK for another one" — a
            #                    UI-safety flag that stops a Back-button
            #                    re-mount from burning an attempt.
            #
            # So they compose rather than compete: no quota → the intent gate
            # decides (unchanged); quota exhausted → refused even WITH
            # `new_attempt: true`, since a deliberate retake request cannot
            # manufacture an entitlement. Deliberately NOT a second copy of
            # the intent gate, and the intent gate was not widened into a
            # quota — `new_attempt` still means exactly what it did.
            #
            # Counts SUBMITTED attempts, not last_attempt.attempt_number:
            # numbering can have gaps (an expired 0-answer ghost is deleted
            # above), and a quota must count real completions.
            #
            # Placed after the resume branch, so a learner mid-attempt on a
            # single-attempt mock can still resume it — the quota bounds how
            # many attempts they get, not whether they may finish one.
            if quiz.max_attempts is not None:
                submitted_count = QuizAttempt.objects.filter(
                    quiz=quiz,
                    learner_profile=learner,
                    status=QuizAttempt.STATUS_SUBMITTED,
                ).count()
                if submitted_count >= quiz.max_attempts:
                    # 200 + `already_submitted`, matching the intent gate's
                    # existing shape so current clients keep routing to the
                    # result screen; `attempts_exhausted` is the new signal
                    # for a client that wants to explain WHY.
                    return Response(
                        {
                            "detail": (
                                "You have used all "
                                f"{quiz.max_attempts} attempt(s) for this test."
                            ),
                            "already_submitted": True,
                            "attempts_exhausted": True,
                            "max_attempts": quiz.max_attempts,
                            "attempts_used": submitted_count,
                            "attempt_id": (
                                last_attempt.id if last_attempt else None
                            ),
                            "attempt_number": (
                                last_attempt.attempt_number
                                if last_attempt else None
                            ),
                            "started_at": (
                                last_attempt.started_at if last_attempt else None
                            ),
                            "expires_at": None,
                        },
                        status=status.HTTP_200_OK,
                    )

            # A retake over a finished attempt must be deliberate — see the
            # class docstring. Only guards the RETAKE case: with no prior
            # attempt at all, last_attempt is None and the first attempt is
            # created as before, so a learner starting a quiz normally is
            # unaffected and no caller needs updating for that path.
            if (
                last_attempt is not None
                and last_attempt.status == QuizAttempt.STATUS_SUBMITTED
                and not request.data.get("new_attempt")
            ):
                return Response(
                    {
                        "detail": "You have already submitted this quiz.",
                        "already_submitted": True,
                        "attempt_id": last_attempt.id,
                        "attempt_number": last_attempt.attempt_number,
                        "started_at": last_attempt.started_at,
                        "expires_at": None,
                    },
                    status=status.HTTP_200_OK,
                )

            new_attempt_number = (
                last_attempt.attempt_number + 1) if last_attempt else 1

            new_attempt = QuizAttempt.objects.create(
                quiz=quiz,
                student=request.user,
                learner_profile=learner,
                attempt_number=new_attempt_number
            )

        return Response(
            {
                "detail": "Quiz started successfully.",
                "attempt_id": new_attempt.id,
                "started_at": new_attempt.started_at,
                "expires_at": (
                    new_attempt.started_at + timedelta(minutes=quiz.time_limit_minutes)
                    if quiz.time_limit_minutes else None
                ),
            },
            status=status.HTTP_200_OK,
        )


class SubmitQuizView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, pk):
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject__course__board"),
            pk=pk,
            is_assigned=True,
        )

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        # A PENDING attempt can only exist via StartQuizView, which already
        # gates this — checked again here for the same reason
        # SubmitAssignmentView re-checks: submit shouldn't trust that create
        # was the only door.
        _assert_learner_may_see_quiz(learner, quiz)

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
        quiz = get_object_or_404(Quiz, pk=pk, is_assigned=True)

        if quiz.quiz_type != Quiz.TYPE_PRACTICE:
            raise PermissionDenied(
                "Instant feedback is only available in practice-mode quizzes."
            )

        # Phase 4: a practice quiz may also be configured to hold answers
        # back (`reveal_answers` = after_submit / never). Checked through the
        # model helper so this endpoint and QuizResultView can never disagree
        # about what the two reveal fields mean.
        if not quiz.instant_feedback_enabled:
            raise PermissionDenied(
                "This quiz does not reveal answers until it is submitted."
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

        # StartQuizView already required an active subscription to create
        # the attempt, but a practice attempt is untimed and quizzes never
        # expire — re-checking here closes the gap where a subscription
        # lapses while an attempt sits open indefinitely.
        from enrollments.services import has_active_subscription
        if not has_active_subscription(
            user=request.user, course=quiz.subject.course, learner_profile=learner,
        ):
            raise PermissionDenied("Your subscription for this course has expired.")

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

        # First answer is final — the frontend already enforces this client
        # side (feedback locks the question), this closes the same rule
        # server side so a direct API call can't re-answer after seeing the
        # correct choice, which would otherwise let practice "accuracy"
        # (fed into teacher-facing analytics) be gamed for free.
        existing = StudentAnswer.objects.filter(
            attempt=attempt, question=question
        ).first()
        if existing:
            raise PermissionDenied("This question has already been answered.")

        StudentAnswer.objects.create(
            attempt=attempt, question=question,
            selected_choice=choice,
            is_correct=choice.is_correct,
            time_spent_seconds=time_spent,
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
            .select_related("subject", "subject__course__board", "created_by")
            .prefetch_related("questions__choices"),
            pk=pk,
            is_assigned=True,
        )

        # The owning teacher gets the full question set INCLUDING is_correct
        # and the explanation. This endpoint used to hand the teacher the
        # student serializer, which deliberately strips both — so the
        # teacher's own "View quiz" screen could never highlight an answer or
        # render the "Correct answer" pill, on a quiz they wrote themselves.
        # Ownership is already enforced immediately below, and the student
        # branch is untouched.
        if _in_teacher_context(request):
            if quiz.created_by != request.user:
                raise PermissionDenied("Not authorized for this quiz.")

            from .serializers import QuizDetailTeacherSerializer
            return Response(
                QuizDetailTeacherSerializer(quiz, context={"request": request}).data
            )
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
            _assert_learner_may_see_quiz(learner, quiz)

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
            .select_related("subject", "subject__course__board", "created_by")
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
                "subject", "subject__course__board", "created_by"),
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

        # Retakes are unlimited by design (see StartQuizView) — the answer
        # key is only revealed on a student's first `reveal_answers_after`
        # attempts, so reading it can't be combined with a fresh retake for
        # a free score. Practice mode is exempt: instant per-question
        # feedback is that mode's whole point, already scoped separately
        # (CheckAnswerView), not this end-of-attempt review.
        #
        # That rule now also has to respect `reveal_answers="never"`, so it
        # lives on the model (Quiz.answers_revealed_for) rather than inline
        # here — one place where the timing mode and the attempt budget
        # combine. Same behaviour as before for every existing quiz.
        answers_revealed = quiz.answers_revealed_for(attempt.attempt_number)

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
                "correct_choice": (
                    correct_choice.text
                    if answers_revealed and correct_choice else ""
                ),
                "is_correct": answer.is_correct,
                "explanation": q.explanation if answers_revealed else "",
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
        scored_higher_than = 0.0
        if quiz.total_marks and all_scores:
            pct_scores = [s * 100.0 / quiz.total_marks for s in all_scores]
            class_avg_percent = round(sum(pct_scores) / len(pct_scores), 1)
            better_count = sum(1 for s in all_scores if s > attempt.score)
            percentile = round(better_count * 100.0 / len(all_scores), 1)
            worse_count = sum(1 for s in all_scores if s < attempt.score)
            scored_higher_than = round(worse_count * 100.0 / len(all_scores), 1)

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
            "course_title": quiz.subject.course.title if quiz.subject.course_id else "",
            "board_name": board_name_via(quiz, "subject", "course"),
            "teacher_name": quiz.created_by.email if quiz.created_by else "",
            "quiz_type": quiz.quiz_type,
            "total_marks": quiz.total_marks,
            "score": attempt.score,
            "submitted_at": attempt.submitted_at,
            "attempt_number": attempt.attempt_number,
            "answers_revealed": answers_revealed,
            # `questions` below only holds the questions this attempt ANSWERED
            # (it is built from attempt.answers). The result screen used its
            # length as the denominator for accuracy, so answering 5 of 20
            # correctly rendered "100%" directly above "5 / 20 marks", and a
            # timer expiry with zero answers rendered "NaN%". questions_total
            # is the paper's real size; `questions` stays answered-only so the
            # topic/difficulty breakdowns keep measuring what was attempted.
            "questions_total": quiz.questions.count(),
            "questions": result_questions,
            "class_avg_percent": class_avg_percent,
            "percentile": percentile,
            "scored_higher_than": scored_higher_than,
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
        from accounts.auth_flow import get_active_profile

        # Scoped to the ACTIVE PROFILE, not the account — every other
        # student view in this file does this (e.g. StudentDashboardView);
        # filtering on `course__subscriptions__user` alone leaked sibling
        # profiles' subject/teacher metadata into each other's picker.
        learner = get_active_profile(request)
        if learner is None:
            return Response([], status=403)

        subjects = (
            Subject.objects
            .filter(
                course__subscriptions__learner_profile=learner,
                course__subscriptions__status=_Sub.STATUS_ACTIVE,
                course__subscriptions__expires_at__gt=_tz.now(),
            )
            .select_related("course__board")
            .prefetch_related("teaching_assignments__teacher")
            .distinct()
        )

        data = []
        for subject in subjects:
            teacher_rel = next(
                (ta for ta in subject.teaching_assignments.all()
                 if ta.batch_id is None and ta.is_active),
                None,
            )
            teacher_name = (
                teacher_rel.teacher.email if teacher_rel else ""
            )
            data.append({
                "id": subject.id,
                "subject": subject.name,
                # This picker spans every enrolled course, so "Mathematics"
                # legitimately appears more than once — the course+board pair
                # is the only thing that tells the two rows apart.
                "course_title": subject.course.title if subject.course_id else "",
                "board_name": board_name_via(subject, "course"),
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
        quiz = get_object_or_404(
            Quiz.objects.select_related("subject"), pk=pk, is_assigned=True,
        )

        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        # Pre-existing cross-batch leak, fixed here: this was the ONE student
        # per-object quiz endpoint with no batch check at all, so a Batch-B
        # learner holding a Batch-A quiz UUID got its title, quiz_type and
        # total_marks back (and confirmation the quiz exists). Every sibling
        # endpoint — StartQuizView, SubmitQuizView, QuizDetailView — has
        # asserted this since Quiz.batch started being written.
        _assert_learner_may_see_quiz(learner, quiz)

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

        if not teaches_subject(user, quiz.subject):
            raise PermissionDenied("Not assigned to this subject.")

        # Grouped by LEARNER PROFILE, not by account (theme T2). One email can
        # own several profiles — siblings on a parent account. This used to
        # group on student_id and name the row from the account's DEFAULT
        # profile, so Riya (default, 4/10) and her brother Arjun (10/10)
        # collapsed into a single row reading "Riya · 2 attempts · best 10/10".
        # Legacy attempts written before QuizAttempt.learner_profile existed
        # have it NULL; those still group per account (there is nothing finer
        # to group them by) and are named from the default profile as before.
        summaries = list(
            QuizAttempt.objects
            .filter(quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED)
            .values("learner_profile_id", "student_id", "student__email")
            .annotate(
                latest_submitted_at=Max("submitted_at"),
                best_score=Max("score"),
                average_score=Avg("score"),
                attempts_count=Count("id"),
            )
            .order_by("student__email")
        )

        from accounts.models import LearnerProfile

        # Names for the profiles that actually took an attempt…
        profile_ids = [s["learner_profile_id"] for s in summaries if s["learner_profile_id"]]
        profile_names = {
            lp.id: (lp.full_name or "").strip() or lp.display_name
            for lp in LearnerProfile.objects.filter(id__in=profile_ids)
        }
        # …and the default-profile fallback, for legacy NULL rows only.
        legacy_account_ids = [
            s["student_id"] for s in summaries if not s["learner_profile_id"]
        ]
        default_names = {}
        if legacy_account_ids:
            for lp in (
                LearnerProfile.objects
                .filter(account_id__in=legacy_account_ids, is_active=True)
                .order_by("account_id", "-is_default", "created_at")
            ):
                default_names.setdefault(
                    lp.account_id, (lp.full_name or "").strip() or lp.display_name
                )

        data = []
        for s in summaries:
            lp_id = s["learner_profile_id"]
            name = (
                profile_names.get(lp_id) if lp_id
                else default_names.get(s["student_id"])
            )
            data.append({
                # The drill-down key. It is the LEARNER PROFILE id whenever
                # there is one — TeacherStudentAttemptsView accepts either
                # (see its queryset), so a legacy row keeps working on the
                # account id.
                "student_id": lp_id or s["student_id"],
                "learner_profile_id": lp_id,
                "account_id": s["student_id"],
                "student_name": name or s["student__email"],
                "student_email": s["student__email"],
                "latest_submitted_at": s["latest_submitted_at"],
                "best_score": s["best_score"],
                "average_score": round(s["average_score"] or 0, 2),
                "attempts_count": s["attempts_count"],
                "total_marks": quiz.total_marks,
            })

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

        if not teaches_subject(user, quiz.subject):
            raise PermissionDenied("Not assigned to this subject.")

        # `student_id` in the URL is whatever key TeacherQuizAttemptsView put
        # on the roster row: a LEARNER PROFILE id for any modern attempt, or
        # the account id for legacy rows that predate
        # QuizAttempt.learner_profile. Matching either keeps sibling rows
        # separate (a profile id can only ever match one child) without
        # breaking the legacy drill-down. The two ids come from different
        # tables, so a UUID can't be ambiguous between them.
        return (
            QuizAttempt.objects
            .filter(
                Q(learner_profile_id=student_id)
                | Q(learner_profile__isnull=True, student_id=student_id),
                quiz=quiz,
                status=QuizAttempt.STATUS_SUBMITTED,
            )
            .select_related("student", "learner_profile")
            .order_by("attempt_number")
        )


class TeacherQuizAttemptDetailView(APIView):
    # IsTeacherContext is NOT optional here even though teaches_subject() is
    # checked below: this endpoint returns another learner's full name, score
    # and every answer they gave. Without the context gate, a teacher whose
    # own child uses the same browser profile could read any classmate's
    # attempt while switched into LEARNER context — i.e. without ever passing
    # the teacher-password gate. Every other teacher endpoint in this file
    # already requires it; this one was the only omission.
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get(self, request, pk):
        attempt = get_object_or_404(
            QuizAttempt.objects
            .select_related("student", "learner_profile", "quiz")
            .prefetch_related(
                "answers__question__choices",
                "answers__selected_choice",
            ),
            id=pk
        )

        if not teaches_subject(request.user, attempt.quiz.subject):
            raise PermissionDenied("Not authorized.")

        # Keyed by question so SKIPPED questions can be folded back in below.
        answer_by_question = {a.question_id: a for a in attempt.answers.all()}

        result_questions = []

        # Iterate the QUIZ's questions, not the attempt's answers. Iterating
        # answers meant an unanswered question simply vanished from the
        # review, so a 3-of-10 attempt rendered as a 3-question quiz and the
        # teacher had no way to see what the student skipped.
        for question in attempt.quiz.questions.all().order_by("order"):
            correct_choice = next(
                (c for c in question.choices.all() if c.is_correct), None
            )
            answer = answer_by_question.get(question.id)
            result_questions.append({
                "question": question.text,
                "options": [c.text for c in question.choices.all()],
                # None (not "") for a skip, so the client can tell "left
                # blank" apart from "chose an option whose text is empty".
                "selected": answer.selected_choice.text if answer else None,
                "correct": correct_choice.text if correct_choice else "",
                # Authoritative correctness, straight off StudentAnswer. The
                # review screen used to re-derive this by string-comparing the
                # selected option's TEXT against the correct option's text, so
                # two options worded identically made the review contradict
                # the score that was actually recorded.
                "is_correct": bool(answer and answer.is_correct),
                "answered": answer is not None,
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
            Quiz.objects.select_related("subject", "subject__course__board"), pk=pk
        )

        if not teaches_subject(request.user, quiz.subject):
            raise PermissionDenied("Not assigned to this subject.")

        submitted_attempts = list(
            QuizAttempt.objects.filter(
                quiz=quiz, status=QuizAttempt.STATUS_SUBMITTED,
            ).select_related("student").prefetch_related("answers")
        )

        # One grouped query for the whole item analysis — this used to run two
        # COUNTs per question (51 queries on a 25-question quiz).
        answer_stats = {
            row["question_id"]: row
            for row in (
                StudentAnswer.objects
                .filter(
                    question__quiz=quiz,
                    attempt__status=QuizAttempt.STATUS_SUBMITTED,
                )
                .values("question_id")
                .annotate(
                    total=Count("id"),
                    correct=Count("id", filter=Q(is_correct=True)),
                )
            )
        }

        items = []
        for q in quiz.questions.all().order_by("order"):
            row = answer_stats.get(q.id)
            total = row["total"] if row else 0
            correct = row["correct"] if row else 0
            items.append({
                "id": q.id,
                "order": q.order + 1,
                "text": q.text,
                "pct_correct": round(correct * 100.0 / total, 1) if total else 0,
                # How many submitted attempts actually answered this question.
                # The client needs it to tell "0% got this right" apart from
                # "nobody has answered this yet" — pct_correct is 0 in both
                # cases, so a brand-new quiz with zero attempts tripped the
                # "< 40% correct" rule on every single question and reported
                # "Flagged questions: 25".
                "answers_count": total,
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

        # Half-open buckets [lo, hi), with the top one closed at 100. The old
        # inclusive ranges — (0,20), (21,40), (41,60)… — left the open
        # intervals 20–21, 40–41, 60–61 and 80–81 in NO bucket at all, so a
        # score of 20.5% (a real value: pct_scores are floats) was silently
        # dropped and the chart's columns summed to less than the attempt
        # count.
        buckets = [("0–19", 0, 20), ("20–39", 20, 40), ("40–59", 40, 60), ("60–79", 60, 80)]
        # Negative marking (Phase 4) lets pct_scores go below 0, which used to
        # fall into none of the buckets below and silently vanish from the
        # chart — the columns would then sum to less than the attempt count,
        # the same failure mode the half-open-range fix above already solved
        # for the 20/40/60/80 boundaries.
        #
        # Emitted ONLY when something actually scored below zero. Negative
        # marking is mock-only, so an unconditional bucket would put a
        # permanently-empty "Below 0" column on every practice quiz's chart.
        # Note this DOES shift the index of every later bucket when present —
        # safe today because QuizAnalytics.jsx keys off each entry's `range`
        # label rather than its position, but a positional consumer would
        # break, so keep reading this list by label.
        below_zero = sum(1 for p in pct_scores if p < 0)
        score_distribution = (
            [{"range": "Below 0", "count": below_zero}] if below_zero else []
        )
        score_distribution += [
            {"range": label, "count": sum(1 for p in pct_scores if lo <= p < hi)}
            for label, lo, hi in buckets
        ]
        # Top bucket is closed at both ends, and also absorbs any >100%
        # outlier produced by a total_marks edit made after an attempt was
        # scored — otherwise that attempt would fall out of the chart too.
        score_distribution.append(
            {"range": "80–100", "count": sum(1 for p in pct_scores if p >= 80)}
        )

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
            Quiz.objects.select_related("subject", "subject__course__board"), pk=pk
        )

        if not teaches_subject(request.user, quiz.subject):
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
                # Without this, a multi-child account's reminder about one
                # sibling's pending quiz leaked onto every other sibling's
                # dashboard too (same M2/Phase 3 §18 profile-isolation gap
                # as notifications/tasks.py's _remind()).
                audience_identity=f"L:{lp.id}",
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
    GET /teacher/question-bank/?scope=mine|school&subject=<id>&topic=&difficulty=&state=&search=

      - scope=mine   → EVERY question on the requesting teacher's own
                       quizzes, regardless of quiz.review_status and
                       regardless of bank_state. Ownership, not admin
                       approval, is what makes a question "yours" (Phase 2 —
                       before this, a teacher's own questions only showed up
                       in their own bank once an admin approved the quiz,
                       which is the exact ownership inversion this refactor
                       removes; see quizzes/visibility.py for the parallel
                       fix already done for quiz-level visibility).
      - scope=school → questions from OTHER teachers assigned to the same
                       subject(s) as the requester, gated on
                       bank_state="accepted" — i.e. the shared ShikshaCom
                       bank an admin has actually curated, not merely "on an
                       approved quiz" (a question can sit on an approved quiz
                       and still be bank_state="private").

    `state=` additionally narrows either scope to one of the four
    `Question.BANK_STATE_*` values, for the T3 state-chip filter row.
    """
    serializer_class = BankQuestionSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        scope = self.request.query_params.get("scope", "mine")
        subject_id = self.request.query_params.get("subject")
        topic = self.request.query_params.get("topic", "").strip()
        difficulty = self.request.query_params.get("difficulty", "").strip()
        state = self.request.query_params.get("state", "").strip()
        search = self.request.query_params.get("search", "").strip()

        assigned_subject_ids = TeachingAssignment.objects.filter(
            teacher=user, is_active=True
        ).values_list("subject_id", flat=True).distinct()

        qs = (
            Question.objects
            .select_related("quiz", "quiz__subject", "quiz__created_by")
            .prefetch_related("choices")
        )

        if scope == "school":
            qs = qs.filter(
                quiz__subject_id__in=assigned_subject_ids,
                bank_state=Question.BANK_STATE_ACCEPTED,
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
        if state:
            qs = qs.filter(bank_state=state)
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

        assigned_subject_ids = TeachingAssignment.objects.filter(
            teacher=user, is_active=True
        ).values_list("subject_id", flat=True).distinct()

        topics = (
            Question.objects
            .filter(quiz__subject_id__in=assigned_subject_ids)
            # Same ownership fix as TeacherQuestionBankView: topics must come
            # from questions the teacher can actually pull into the bank —
            # everything they wrote themselves (any bank_state), plus
            # anyone's already-accepted questions (the "school" scope) — not
            # gated on quiz__review_status, which is informational-only.
            .filter(Q(quiz__created_by=user) | Q(bank_state=Question.BANK_STATE_ACCEPTED))
            .exclude(topic="")
            .values_list("topic", flat=True)
            .distinct()
            .order_by("topic")
        )

        subjects = (
            Subject.objects
            .filter(id__in=assigned_subject_ids)
            .select_related("course__board")
            .order_by("name")
        )
        subjects = [
            {
                "id": s.id,
                "name": s.name,
                "course_title": s.course.title if s.course_id else "",
                "board_name": board_name_via(s, "course"),
            }
            for s in subjects
        ]

        return Response({
            "topics": list(topics),
            "subjects": subjects,
            "difficulties": [c[0] for c in Question.DIFFICULTY_CHOICES],
        })


class TeacherBankSummaryView(APIView):
    """
    GET /teacher/question-bank/summary/

    Counts, by bank_state, over EVERY question the requesting teacher has
    ever written (`quiz__created_by=user`) — never another teacher's. One
    aggregate query (values().annotate(Count)) rather than four separate
    .count() calls, since T3's stat cards, T4's state rows and Phase 6's nav
    pill (`suggested + changes_requested`) all need this on every page load.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified, IsTeacherContext]

    def get(self, request):
        rows = (
            Question.objects
            .filter(quiz__created_by=request.user)
            .values("bank_state")
            .annotate(n=Count("id"))
        )
        by_state = {row["bank_state"]: row["n"] for row in rows}

        accepted = by_state.get(Question.BANK_STATE_ACCEPTED, 0)
        suggested = by_state.get(Question.BANK_STATE_SUGGESTED, 0)
        changes_requested = by_state.get(Question.BANK_STATE_CHANGES_REQUESTED, 0)
        private = by_state.get(Question.BANK_STATE_PRIVATE, 0)

        return Response({
            "total": accepted + suggested + changes_requested + private,
            "accepted": accepted,
            "suggested": suggested,
            "changes_requested": changes_requested,
            "private": private,
        })


class TeacherQuestionBankStateView(APIView):
    """PATCH /teacher/questions/:pk/bank/   {suggest_to_bank: bool}

    The teacher's opt-in/out lever for the shared ShikshaCom bank (README
    "Interactions & behaviour" — optimistic on the client, revert+toast on
    failure). Ownership check follows the same pattern
    TeacherQuizAssignView established for Phase 1: get-or-404, then 403 if
    this teacher didn't write it — via `quiz__created_by`, since Question has
    no `created_by` of its own.

    Turning suggest_to_bank off always moves the question to "private",
    including one an admin had already accepted — see Question.save()'s
    docstring for why that direction is unconditional while the reverse
    (turning it back on) never clobbers an existing admin decision.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def patch(self, request, pk):
        require_teacher_context(request)

        question = get_object_or_404(
            Question.objects.select_related("quiz", "quiz__subject"), pk=pk,
        )
        if question.quiz.created_by != request.user:
            raise PermissionDenied("You did not write this question.")

        serializer = QuestionBankStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        question.suggest_to_bank = serializer.validated_data["suggest_to_bank"]
        question.save()

        return Response(
            BankQuestionSerializer(question, context={"request": request}).data
        )


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
            .select_related("subject", "subject__course__board", "created_by")
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
            .select_related("subject", "subject__course__board", "created_by", "reviewed_by")
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
