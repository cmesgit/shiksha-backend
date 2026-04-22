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


class DashboardView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):

        user = request.user

        enrollments = Enrollment.objects.filter(
            user=user,
            status=Enrollment.STATUS_ACTIVE
        ).values_list("course_id", flat=True)

        is_student = len(enrollments) > 0

        teacher_prefetch = Prefetch(
            "chapter__subject__subject_teachers",
            queryset=SubjectTeacher.objects.select_related("teacher"),
            to_attr="prefetched_teachers",
        )

        EXCLUDED_STATUSES = [
            LiveSession.STATUS_COMPLETED,
            LiveSession.STATUS_CANCELLED,
        ]

        now = timezone.now()

        # =========================
        # 👨‍🎓 STUDENT DASHBOARD
        # =========================
        if is_student:

            course_ids = list(enrollments)

            subject_ids = Subject.objects.filter(
                course_id__in=course_ids
            ).values_list("id", flat=True)

            chapter_ids = Chapter.objects.filter(
                subject_id__in=subject_ids
            ).values_list("id", flat=True)

            today_start = now.replace(
                hour=0, minute=0, second=0, microsecond=0)

            # Sessions card — today + next 7 days
            sessions = (
                LiveSession.objects
                .filter(
                    subject_id__in=subject_ids,
                    start_time__gte=today_start,
                    start_time__lte=today_start + timedelta(days=7),
                )
                .exclude(status__in=EXCLUDED_STATUSES)
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            # FIX 1: all_sessions — from today onwards, not just today
            # Used for calendar dots and schedule panel
            all_sessions = (
                LiveSession.objects
                .filter(
                    subject_id__in=subject_ids,
                    start_time__gte=today_start,
                )
                .exclude(status__in=EXCLUDED_STATUSES)
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            assignments = (
                Assignment.objects
                .filter(chapter_id__in=chapter_ids)
                .select_related("chapter__subject")
                .prefetch_related(teacher_prefetch)
                .order_by("due_date")[:5]
            )

            quizzes = (
                Quiz.objects
                .filter(
                    subject_id__in=subject_ids,
                    is_published=True
                )
                .select_related("created_by")
                .order_by("due_date")[:5]
            )

        # =========================
        # 👨‍🏫 TEACHER DASHBOARD
        # =========================
        else:

            today_start = now.replace(
                hour=0, minute=0, second=0, microsecond=0)

            # Sessions card — today + next 7 days
            sessions = (
                LiveSession.objects
                .filter(
                    created_by=user,
                    start_time__gte=today_start,
                    start_time__lte=today_start + timedelta(days=7),
                )
                .exclude(status__in=EXCLUDED_STATUSES)
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            all_sessions = (
                LiveSession.objects
                .filter(
                    created_by=user,
                    start_time__gte=today_start
                )
                .exclude(status__in=EXCLUDED_STATUSES)
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            assignments = (
                Assignment.objects
                .filter(
                    chapter__subject__subject_teachers__teacher=user
                )
                .select_related("chapter__subject")
                .prefetch_related(teacher_prefetch)
                .distinct()
                .order_by("due_date")
            )

            quizzes = (
                Quiz.objects
                .filter(
                    created_by=user,
                    is_published=True
                )
                .select_related("created_by", "subject")
                .order_by("due_date")
            )

        # =========================
        # 🔔 COMMON
        # =========================

        private_sessions = (
            PrivateSession.objects
            .filter(
                Q(teacher=user) | Q(requested_by=user),
                scheduled_date__gte=now.date(),
                status__in=["pending", "approved", "needs_reconfirmation"]
            )
            .select_related("teacher", "requested_by")
            .order_by("scheduled_date", "scheduled_time")
        )

        # FIX 2: exclude stale past activities from notifications
        notifications = (
            Activity.objects
            .filter(user=user)
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

        # FIX 3: schedule — only future items with actual due dates
        schedule = (
            Activity.objects
            .filter(user=user)
            .exclude(due_date=None)
            .exclude(due_date__lt=now)
            .order_by("due_date")[:10]
        )

        return Response({
            "sessions": DashboardSessionSerializer(sessions, many=True).data,
            "all_sessions": DashboardSessionSerializer(all_sessions, many=True).data,
            "assignments": DashboardAssignmentSerializer(assignments, many=True).data,
            "quizzes": DashboardQuizSerializer(quizzes, many=True).data,
            "private_sessions": DashboardPrivateSessionSerializer(private_sessions, many=True).data,
            "notifications": DashboardActivitySerializer(notifications, many=True).data,
            "schedule": DashboardActivitySerializer(schedule, many=True).data
        })
