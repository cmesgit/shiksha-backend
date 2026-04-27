"""
Dashboard endpoint.

Hardened so that a single failing slice (e.g. a malformed Activity row,
a serializer edge-case, or a stale FK) doesn't take the whole dashboard
down with a 500.  Each block runs inside a guarded helper that logs the
real traceback and falls back to an empty list so the rest of the page
still loads.
"""

import logging

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q, Prefetch

from enrollments.models import Enrollment
from courses.models import Subject, Chapter, SubjectTeacher

from livestream.models import LiveSession
from assignments.models import Assignment
from quizzes.models import Quiz
from activity.models import Activity
from sessions_app.models import PrivateSession

from .serializers import (
    DashboardSessionSerializer,
    DashboardAssignmentSerializer,
    DashboardQuizSerializer,
    DashboardActivitySerializer,
    DashboardPrivateSessionSerializer
)


logger = logging.getLogger(__name__)


def _guard(label, fn, default):
    """Run ``fn`` and return its result, or fall back to ``default``.

    Any exception is logged with full traceback so the server log shows
    exactly which dashboard section blew up.
    """
    try:
        return fn()
    except Exception:
        logger.exception("Dashboard section failed: %s", label)
        return default


def _student_subjects(course_ids):
    return list(Subject.objects.filter(course_id__in=course_ids).values_list("id", flat=True))


def _student_chapters(subject_ids):
    return list(Chapter.objects.filter(subject_id__in=subject_ids).values_list("id", flat=True))


def _live_sessions_for_student(subject_ids, today_start, excluded_statuses, week_only):
    qs = LiveSession.objects.filter(subject_id__in=subject_ids, start_time__gte=today_start)
    if week_only:
        qs = qs.filter(start_time__lte=today_start + timedelta(days=7))
    return list(qs.exclude(status__in=excluded_statuses)
                  .select_related("subject", "created_by")
                  .order_by("start_time"))


def _live_sessions_for_teacher(user, today_start, excluded_statuses, week_only):
    qs = LiveSession.objects.filter(created_by=user, start_time__gte=today_start)
    if week_only:
        qs = qs.filter(start_time__lte=today_start + timedelta(days=7))
    return list(qs.exclude(status__in=excluded_statuses)
                  .select_related("subject", "created_by")
                  .order_by("start_time"))


def _student_assignments(chapter_ids, teacher_prefetch):
    return list(Assignment.objects
                .filter(chapter_id__in=chapter_ids)
                .select_related("chapter__subject")
                .prefetch_related(teacher_prefetch)
                .order_by("due_date")[:5])


def _teacher_assignments(user, teacher_prefetch):
    return list(Assignment.objects
                .filter(chapter__subject__subject_teachers__teacher=user)
                .select_related("chapter__subject")
                .prefetch_related(teacher_prefetch)
                .distinct()
                .order_by("due_date"))


def _student_quizzes(subject_ids):
    return list(Quiz.objects
                .filter(subject_id__in=subject_ids, is_published=True)
                .select_related("created_by")
                .order_by("due_date")[:5])


def _teacher_quizzes(user):
    return list(Quiz.objects
                .filter(created_by=user, is_published=True)
                .select_related("created_by", "subject")
                .order_by("due_date"))


def _private_sessions(user, now):
    return list(PrivateSession.objects
                .filter(Q(teacher=user) | Q(requested_by=user),
                        scheduled_date__gte=now.date(),
                        status__in=["pending", "approved", "needs_reconfirmation"])
                .select_related("teacher", "requested_by")
                .order_by("scheduled_date", "scheduled_time"))


def _notifications(user, now):
    return list(Activity.objects
                .filter(user=user)
                .exclude(type__in=[Activity.TYPE_SESSION,
                                   Activity.TYPE_QUIZ,
                                   Activity.TYPE_ASSIGNMENT],
                         due_date__lt=now)
                .order_by("-created_at")[:10])


def _schedule(user, now):
    return list(Activity.objects
                .filter(user=user)
                .exclude(due_date=None)
                .exclude(due_date__lt=now)
                .order_by("due_date")[:10])


def _enrollments(user):
    return list(Enrollment.objects
                .filter(user=user, status=Enrollment.STATUS_ACTIVE)
                .values_list("course_id", flat=True))


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        course_ids = _guard("enrollments", lambda: _enrollments(user), [])
        is_student = len(course_ids) > 0

        teacher_prefetch = Prefetch(
            "chapter__subject__subject_teachers",
            queryset=SubjectTeacher.objects.select_related("teacher"),
            to_attr="prefetched_teachers",
        )

        excluded_statuses = [LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if is_student:
            subject_ids = _guard("student.subjects", lambda: _student_subjects(course_ids), [])
            chapter_ids = _guard("student.chapters", lambda: _student_chapters(subject_ids), [])
            sessions = _guard("student.sessions",
                              lambda: _live_sessions_for_student(subject_ids, today_start, excluded_statuses, True),
                              [])
            all_sessions = _guard("student.all_sessions",
                                  lambda: _live_sessions_for_student(subject_ids, today_start, excluded_statuses, False),
                                  [])
            assignments = _guard("student.assignments",
                                 lambda: _student_assignments(chapter_ids, teacher_prefetch),
                                 [])
            quizzes = _guard("student.quizzes", lambda: _student_quizzes(subject_ids), [])
        else:
            sessions = _guard("teacher.sessions",
                              lambda: _live_sessions_for_teacher(user, today_start, excluded_statuses, True),
                              [])
            all_sessions = _guard("teacher.all_sessions",
                                  lambda: _live_sessions_for_teacher(user, today_start, excluded_statuses, False),
                                  [])
            assignments = _guard("teacher.assignments",
                                 lambda: _teacher_assignments(user, teacher_prefetch),
                                 [])
            quizzes = _guard("teacher.quizzes", lambda: _teacher_quizzes(user), [])

        private_sessions = _guard("common.private_sessions",
                                  lambda: _private_sessions(user, now),
                                  [])
        notifications = _guard("common.notifications", lambda: _notifications(user, now), [])
        schedule = _guard("common.schedule", lambda: _schedule(user, now), [])

        return Response({
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
            "notifications":    _guard("ser.notifications",
                                       lambda: DashboardActivitySerializer(notifications, many=True).data, []),
            "schedule":         _guard("ser.schedule",
                                       lambda: DashboardActivitySerializer(schedule, many=True).data, []),
        })
