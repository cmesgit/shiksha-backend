"""
dashboard/views.py  ·  FULL REPLACEMENT — context + profile isolated
─────────────────────────────────────────────────────────────────────
WHAT WAS WRONG
  The old view decided "student vs teacher" with
      is_student = account has ANY active Enrollment
  On a one-email / many-personalities account that is always wrong:
  a FACULTY teacher whose child is enrolled opened the *teacher*
  dashboard and got the *child's* sessions and assignments. It also
  unioned every learner profile's enrollments (user=…, never
  learner_profile=…) and silently ignored the ?course_id= the student
  frontend has been sending all along.

WHAT THIS VERSION DOES
  • Branches on the JWT claims the auth flow already issues:
        context        →  "learner" | "teacher" | "account"
        active_profile →  LearnerProfile id   (learner context)
        active_track   →  "academy" | "skill" (teacher context)
    Data never decides identity again.
  • LEARNER: everything is scoped to get_active_profile(request)
    (same helper enrollments/quizzes/assignments already use), and
    ?course_id= is honored after being validated against that
    profile's own enrollments.
  • TEACHER: serves the ACADEMY track only. skill-track requests are
    told to use /skill/teacher/dashboard/ (409, code
    "wrong_dashboard"); an unapproved academy track gets a 403 with
    code "academy_not_approved" so the frontend can render the
    pending / rejected gate instead of an empty page.
  • ACCOUNT context (no profile picked yet) → 409 "profile_required".
  • Notification & schedule slices come from Activity filtered by the
    new audience + learner_profile columns (see activity/models.py in
    this fix set), so Profile A never sees Profile B's feed and the
    teacher feed never bleeds into a learner tab.

Per-slice _guard() hardening from the previous version is preserved.
"""

import logging
from datetime import timedelta

from django.db.models import Q, Prefetch, Count
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from accounts.auth_flow import (
    get_active_profile,
    CTX_LEARNER,
    CTX_TEACHER,
)
from accounts.models import Role
from activity.models import Activity
from assignments.models import Assignment, AssignmentSubmission
from courses.models import Subject, Chapter, TeachingAssignment
from courses.progress_stats import average_quiz_score_pct
from enrollments.models import Enrollment
from livestream.models import LiveSession
from quizzes.models import Quiz, QuizAttempt
from sessions_app.models import PrivateSession

from .serializers import (
    DashboardSessionSerializer,
    DashboardAssignmentSerializer,
    DashboardQuizSerializer,
    DashboardActivitySerializer,
    DashboardPrivateSessionSerializer,
    DashboardGradingItemSerializer,
)

logger = logging.getLogger(__name__)


def _guard(label, fn, default):
    try:
        return fn()
    except Exception:
        logger.exception("Dashboard section failed: %s", label)
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Learner slices — every query hangs off the active LearnerProfile
# ─────────────────────────────────────────────────────────────────────────────

def _learner_course_ids(profile, course_id=None):
    """ACTIVE enrollments for THIS profile only.  If the frontend sent
    ?course_id=, narrow to it — but only when it belongs to this profile,
    otherwise fall back to all of the profile's courses (never 404 the
    whole dashboard over a stale localStorage course id)."""
    qs = Enrollment.objects.filter(
        learner_profile=profile,
        status=Enrollment.STATUS_ACTIVE,
    ).values_list("course_id", flat=True)
    ids = list(qs)
    if course_id and str(course_id) in {str(i) for i in ids}:
        return [course_id]
    return ids


def _subjects_for(course_ids):
    return list(
        Subject.objects.filter(course_id__in=course_ids)
        .values_list("id", flat=True)
    )


def _chapters_for(subject_ids):
    return list(
        Chapter.objects.filter(subject_id__in=subject_ids)
        .values_list("id", flat=True)
    )


def _live_sessions_for_subjects(subject_ids, today_start, excluded, week_only,
                                batch_q=None):
    # batch_q: the same Q the assignment and quiz slices already use. The
    # earlier batch fix built _batch_visibility_q and applied it to those two
    # but skipped this slice, so the dashboard kept showing another batch's
    # timetable. LiveSession.batch exists for exactly this reason.
    qs = LiveSession.objects.filter(
        subject_id__in=subject_ids, start_time__gte=today_start
    )
    if batch_q is not None:
        qs = qs.filter(batch_q)
    if week_only:
        qs = qs.filter(start_time__lte=today_start + timedelta(days=7))
    return list(
        qs.exclude(status__in=excluded)
        .select_related("subject", "created_by")
        .order_by("start_time")
    )


def _batch_ids_for(profile, course_ids):
    """This profile's batch in each of its courses, as {course_id: batch_id}.

    A course the learner is enrolled in with no batch assigned maps to None,
    which the callers below treat as "show every batch" — the same deliberate
    over-share assignments/views.py documents: we cannot tell which cohort an
    unplaced learner belongs to, so hiding batch-scoped work would make it
    vanish with no notification.
    """
    return dict(
        Enrollment.objects.filter(
            learner_profile=profile,
            course_id__in=course_ids,
            status=Enrollment.STATUS_ACTIVE,
        ).values_list("course_id", "batch_id")
    )


def _batch_visibility_q(batch_ids_by_course, course_field):
    """Q object matching course-wide items plus this learner's own batch's.

    Mirrors CourseAssignmentsView / StudentCourseMaterials exactly:
    `Q(batch__isnull=True) | Q(batch_id=<my batch for THAT course>)`. Built
    per course because the dashboard spans several at once, and a learner can
    sit in a different batch in each.
    """
    q = Q(batch__isnull=True)
    for course_id, batch_id in batch_ids_by_course.items():
        if batch_id is None:
            # Unplaced in this course → don't restrict it at all.
            q |= Q(**{course_field: course_id})
        else:
            q |= Q(**{course_field: course_id, "batch_id": batch_id})
    return q


def _learner_assignments(chapter_ids, teacher_prefetch, batch_q, submitted_ids):
    """Assignments still outstanding for this learner.

    Two things this used to get wrong, both reported from production:

    1. NO BATCH FILTER. It scoped on chapter only, so a subject with a
       Morning and an Evening batch showed BOTH batches' assignments on the
       dashboard — while the Assignments tab, which goes through
       CourseAssignmentsView, correctly showed one. Same widget, two
       different answers.
    2. SUBMITTED WORK NEVER DROPPED OFF. Nothing here consulted
       AssignmentSubmission, so an assignment the learner had already turned
       in sat on the dashboard forever. Quizzes behave correctly, which is
       what made it obvious something was wrong here.
    """
    return list(
        Assignment.objects.filter(chapter_id__in=chapter_ids, is_published=True)
        .filter(batch_q)
        .exclude(id__in=submitted_ids)
        .select_related("chapter__subject")
        .prefetch_related(teacher_prefetch)
        .order_by("due_date")[:20]
    )


def _submitted_assignment_ids(profile, chapter_ids):
    """Assignments this PROFILE has already submitted.

    Keyed on learner_profile, never on the account: AssignmentSubmission
    carries both, and `student` is the account kept for audit. Two siblings
    on one parent email share an account, so keying on it would let one
    child's submission clear the other child's dashboard.
    """
    return set(
        AssignmentSubmission.objects.filter(
            learner_profile=profile,
            assignment__chapter_id__in=chapter_ids,
        ).values_list("assignment_id", flat=True)
    )


def _learner_quizzes(subject_ids, batch_q):
    # Same batch leak as assignments had — Quiz.batch was equally unfiltered
    # here. Not user-reported yet only because this widget shows no due date.
    return list(
        Quiz.objects.filter(subject_id__in=subject_ids, is_published=True)
        .filter(batch_q)
        .select_related("created_by")
        .order_by("-created_at")[:20]
    )


def _learner_quiz_avg_pct(profile, subject_ids):
    """Average score % across this profile's own SUBMITTED QuizAttempts for
    quizzes in `subject_ids` — the same subject set _learner_quizzes uses,
    which may span every course this profile is enrolled in (not just one).
    Scoped by learner_profile only, matching how the rest of this view's
    learner slices (e.g. _learner_course_ids) key off the active profile —
    unlike courses.progress_stats' single-course stats block, there's no
    dual-key/is_default legacy fallback here. None if there are zero
    submitted attempts. See courses.progress_stats.average_quiz_score_pct
    for the shared percentage math (skips quizzes with total_marks=0)."""
    attempts = list(
        QuizAttempt.objects.filter(
            learner_profile=profile,
            quiz__subject_id__in=subject_ids,
            status=QuizAttempt.STATUS_SUBMITTED,
        )
        # A SUBMITTED attempt only counts as completed if it has answers —
        # matches the ghost-attempt guard in quizzes/views.py's
        # StudentDashboardView and courses.progress_stats.build_progress_stats.
        .annotate(_answer_count=Count("answers"))
        .filter(_answer_count__gt=0)
        .select_related("quiz")
    )
    return average_quiz_score_pct(attempts)


def _learner_private_sessions(user, now):
    # PrivateSession has no learner_profile FK yet (account-level by
    # design of that model) — scope to sessions this ACCOUNT requested,
    # never ones it teaches. See AUDIT.md → "known model gaps".
    # localtime: scheduled_date is an IST-calendar DateField — comparing
    # against raw (UTC) now.date() shows yesterday's already-elapsed
    # sessions as "upcoming" for up to ~5.5h after IST midnight.
    return list(
        PrivateSession.objects.filter(
            requested_by=user,
            scheduled_date__gte=timezone.localtime(now).date(),
            status__in=["pending", "approved", "needs_reconfirmation"],
        )
        .select_related("teacher", "requested_by")
        .order_by("scheduled_date", "scheduled_time")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Teacher slices — ACADEMY track, authored-by-me only
# ─────────────────────────────────────────────────────────────────────────────

def _teacher_live_sessions(user, today_start, excluded, week_only):
    qs = LiveSession.objects.filter(created_by=user, start_time__gte=today_start)
    if week_only:
        qs = qs.filter(start_time__lte=today_start + timedelta(days=7))
    return list(
        qs.exclude(status__in=excluded)
        .select_related("subject", "created_by", "batch", "course")
        .order_by("start_time")
    )


def _teacher_assignments(user, teacher_prefetch):
    return list(
        Assignment.objects.filter(
            chapter__subject__teaching_assignments__teacher=user,
            chapter__subject__teaching_assignments__is_active=True,
        )
        .select_related("chapter__subject")
        .prefetch_related(teacher_prefetch)
        .distinct()
        .order_by("due_date")
    )


def _teacher_quizzes(user):
    return list(
        Quiz.objects.filter(created_by=user, is_published=True)
        .select_related("created_by", "subject")
        .order_by("-created_at")
    )


def _teacher_private_sessions(user, now):
    # localtime: see _learner_private_sessions above.
    return list(
        PrivateSession.objects.filter(
            teacher=user,
            scheduled_date__gte=timezone.localtime(now).date(),
            status__in=["pending", "approved", "needs_reconfirmation"],
        )
        .select_related("teacher", "requested_by")
        .order_by("scheduled_date", "scheduled_time")
    )


def _teacher_grading_queue(user, limit=15):
    """
    Assignment submissions on this teacher's assignments awaiting review,
    newest first. Assignments carry no graded flag, so the queue surfaces
    real submissions rather than a synthetic 'ungraded' subset — the
    "Grade" button opens the submissions view where the teacher reviews
    them. Capped so the dashboard card stays light.
    """
    return list(
        AssignmentSubmission.objects.filter(
            assignment__chapter__subject__teaching_assignments__teacher=user,
            assignment__chapter__subject__teaching_assignments__is_active=True,
        )
        .select_related("student", "assignment__chapter__subject")
        .distinct()
        .order_by("-submitted_at")[:limit]
    )


def _teacher_grading_count(user):
    """Uncapped count for the SAME filter _teacher_grading_queue uses, so
    the '{n} pending' badge doesn't silently cap at that function's
    display-list limit (15) once a teacher has more submissions than fit
    on the card. AssignmentSubmission has no graded/ungraded status field
    (see the docstring above), so — same as the display list — 'pending'
    here means 'submission exists on one of my assignments', not
    'ungraded'; there is currently no way to distinguish the two."""
    return (
        AssignmentSubmission.objects.filter(
            assignment__chapter__subject__teaching_assignments__teacher=user,
            assignment__chapter__subject__teaching_assignments__is_active=True,
        )
        .distinct()
        .count()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Activity feed slices — audience + profile aware
# ─────────────────────────────────────────────────────────────────────────────

def _activity_base(user, *, audience, profile):
    """Rows for one identity of this account.

    audience  Activity.AUDIENCE_LEARNER / AUDIENCE_TEACHER
    profile   LearnerProfile or None (teacher context)

    Legacy rows written before the migration have learner_profile NULL;
    the data migration backfills audience from type, and NULL-profile
    learner rows stay visible to every profile of the account until
    they age out — a deliberate no-data-loss choice.
    """
    qs = Activity.objects.filter(user=user, audience=audience)
    if audience == Activity.AUDIENCE_LEARNER:
        qs = qs.filter(
            Q(learner_profile=profile) | Q(learner_profile__isnull=True)
        )
    else:
        qs = qs.filter(learner_profile__isnull=True)
    return qs


def _notifications(user, now, *, audience, profile):
    return list(
        _activity_base(user, audience=audience, profile=profile)
        .exclude(
            type__in=[
                Activity.TYPE_SESSION,
                Activity.TYPE_QUIZ,
                Activity.TYPE_ASSIGNMENT,
            ],
            due_date__lt=now,
        )
        .order_by("-created_at")[:10]
    )


def _schedule(user, now, *, audience, profile):
    return list(
        _activity_base(user, audience=audience, profile=profile)
        .exclude(due_date=None)
        .exclude(due_date__lt=now)
        .order_by("due_date")[:10]
    )


# ─────────────────────────────────────────────────────────────────────────────
# The view
# ─────────────────────────────────────────────────────────────────────────────

class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        token = getattr(request, "auth", None)
        context = token.get("context") if token else None
        active_track = token.get("active_track") if token else None

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        excluded = [LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]

        teacher_prefetch = Prefetch(
            "chapter__subject__teaching_assignments",
            queryset=TeachingAssignment.objects.filter(
                batch__isnull=True, is_active=True,
            ).select_related("teacher"),
            to_attr="prefetched_teachers",
        )

        # ── TEACHER context ─────────────────────────────────────────────
        if context == CTX_TEACHER:
            if not user.has_role(Role.TEACHER):
                return Response(
                    {"code": "not_teacher", "detail": "No active teacher role."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            teacher = getattr(user, "teacher_profile", None)

            # This endpoint is the ACADEMY (faculty) dashboard. The skill
            # side has its own, already profile/track-correct endpoint.
            if active_track == "skill":
                return Response(
                    {
                        "code": "wrong_dashboard",
                        "detail": "Skill-track dashboards use /skill/teacher/dashboard/.",
                        "use": "/skill/teacher/dashboard/",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            if teacher is None or teacher.academy_status != teacher.TRACK_APPROVED:
                return Response(
                    {
                        "code": "academy_not_approved",
                        "academy_status": getattr(teacher, "academy_status", "locked"),
                        "rejection_reason": getattr(teacher, "academy_rejection_reason", ""),
                        "detail": "Academy (faculty) track is not approved for this account.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            sessions = _guard(
                "teacher.sessions",
                lambda: _teacher_live_sessions(user, today_start, excluded, True), [])
            all_sessions = _guard(
                "teacher.all_sessions",
                lambda: _teacher_live_sessions(user, today_start, excluded, False), [])
            assignments = _guard(
                "teacher.assignments",
                lambda: _teacher_assignments(user, teacher_prefetch), [])
            quizzes = _guard("teacher.quizzes", lambda: _teacher_quizzes(user), [])
            private_sessions = _guard(
                "teacher.private_sessions",
                lambda: _teacher_private_sessions(user, now), [])
            grading_queue = _guard(
                "teacher.grading_queue",
                lambda: _teacher_grading_queue(user), [])
            grading_count = _guard(
                "teacher.grading_count",
                lambda: _teacher_grading_count(user), 0)
            quiz_avg_pct = None  # teacher dashboards have no quiz score to average
            notifications = _guard(
                "teacher.notifications",
                lambda: _notifications(user, now,
                                       audience=Activity.AUDIENCE_TEACHER,
                                       profile=None), [])
            schedule = _guard(
                "teacher.schedule",
                lambda: _schedule(user, now,
                                  audience=Activity.AUDIENCE_TEACHER,
                                  profile=None), [])
            meta = {
                "context": "teacher",
                "active_track": active_track or "academy",
                "profile": None,
                "course": None,
            }

        # ── LEARNER context ─────────────────────────────────────────────
        elif context == CTX_LEARNER:
            profile = get_active_profile(request)
            if profile is None:
                return Response(
                    {"code": "profile_required",
                     "detail": "Select a learner profile."},
                    status=status.HTTP_409_CONFLICT,
                )

            course_id = request.query_params.get("course_id") or None
            course_ids = _guard(
                "learner.enrollments",
                lambda: _learner_course_ids(profile, course_id), [])
            subject_ids = _guard(
                "learner.subjects", lambda: _subjects_for(course_ids), [])
            chapter_ids = _guard(
                "learner.chapters", lambda: _chapters_for(subject_ids), [])

            # Batch scoping is resolved here rather than below, because the
            # session slices need it too — see _live_sessions_for_subjects.
            batch_ids_by_course = _guard(
                "learner.batches",
                lambda: _batch_ids_for(profile, course_ids), {})
            session_batch_q = _batch_visibility_q(
                batch_ids_by_course, "course_id")

            sessions = _guard(
                "learner.sessions",
                lambda: _live_sessions_for_subjects(
                    subject_ids, today_start, excluded, True, session_batch_q), [])
            all_sessions = _guard(
                "learner.all_sessions",
                lambda: _live_sessions_for_subjects(
                    subject_ids, today_start, excluded, False, session_batch_q), [])
            # "Already done" exclusion for the coursework widgets, plus the
            # per-model batch Qs. batch_ids_by_course is resolved above (the
            # session slices need it first). Both default to permissive on
            # failure (_guard's fallbacks) so a hiccup can never blank the
            # dashboard.
            assignment_batch_q = _batch_visibility_q(
                batch_ids_by_course, "chapter__subject__course_id")
            quiz_batch_q = _batch_visibility_q(
                batch_ids_by_course, "subject__course_id")
            submitted_ids = _guard(
                "learner.submitted_assignments",
                lambda: _submitted_assignment_ids(profile, chapter_ids), set())

            assignments = _guard(
                "learner.assignments",
                lambda: _learner_assignments(
                    chapter_ids, teacher_prefetch, assignment_batch_q, submitted_ids), [])
            quizzes = _guard(
                "learner.quizzes",
                lambda: _learner_quizzes(subject_ids, quiz_batch_q), [])
            quiz_avg_pct = _guard(
                "learner.quiz_avg_pct",
                lambda: _learner_quiz_avg_pct(profile, subject_ids), None)
            private_sessions = _guard(
                "learner.private_sessions",
                lambda: _learner_private_sessions(user, now), [])
            grading_queue = []  # learner dashboards have no grading queue
            grading_count = 0   # ditto
            notifications = _guard(
                "learner.notifications",
                lambda: _notifications(user, now,
                                       audience=Activity.AUDIENCE_LEARNER,
                                       profile=profile), [])
            schedule = _guard(
                "learner.schedule",
                lambda: _schedule(user, now,
                                  audience=Activity.AUDIENCE_LEARNER,
                                  profile=profile), [])
            meta = {
                "context": "learner",
                "active_track": None,
                "profile": {
                    "id": str(profile.id),
                    "display_name": profile.display_name,
                },
                "course": str(course_ids[0]) if course_id and course_ids == [course_id] else None,
            }

        # ── ACCOUNT context (nothing picked yet) ────────────────────────
        else:
            return Response(
                {"code": "profile_required",
                 "detail": "Pick a learner profile or enter teacher mode first."},
                status=status.HTTP_409_CONFLICT,
            )

        notifications_data = _guard(
            "ser.notifications",
            lambda: DashboardActivitySerializer(notifications, many=True).data,
            [],
        )
        unread_count = sum(1 for n in notifications_data if n.get("unread"))

        return Response({
            "meta":             meta,
            "sessions":         _guard("ser.sessions",
                                       lambda: DashboardSessionSerializer(sessions, many=True).data, []),
            "all_sessions":     _guard("ser.all_sessions",
                                       lambda: DashboardSessionSerializer(all_sessions, many=True).data, []),
            "assignments":      _guard("ser.assignments",
                                       lambda: DashboardAssignmentSerializer(assignments, many=True).data, []),
            "quizzes":          _guard("ser.quizzes",
                                       lambda: DashboardQuizSerializer(quizzes, many=True).data, []),
            "private_sessions": _guard("ser.private_sessions",
                                       lambda: DashboardPrivateSessionSerializer(private_sessions, many=True).data, []),
            "grading_queue":    _guard("ser.grading_queue",
                                       lambda: DashboardGradingItemSerializer(grading_queue, many=True).data, []),
            "grading_count":    grading_count,
            "quiz_avg_pct":     quiz_avg_pct,
            "notifications":    notifications_data,
            "schedule":         _guard("ser.schedule",
                                       lambda: DashboardActivitySerializer(schedule, many=True).data, []),
            "unread_count":     unread_count,
        })
