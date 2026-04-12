from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from datetime import timedelta
from django.utils import timezone

from enrollments.models import Enrollment
from courses.models import Subject, Chapter

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

        # 🔥 detect student
        is_student = Enrollment.objects.filter(
            user=user,
            status=Enrollment.STATUS_ACTIVE
        ).exists()

        # =========================
        # 👨‍🎓 STUDENT DASHBOARD
        # =========================
        if is_student:

            course_ids = Enrollment.objects.filter(
                user=user,
                status=Enrollment.STATUS_ACTIVE
            ).values_list("course_id", flat=True)

            subject_ids = Subject.objects.filter(
                course_id__in=course_ids
            ).values_list("id", flat=True)

            chapter_ids = Chapter.objects.filter(
                subject_id__in=subject_ids
            ).values_list("id", flat=True)

            today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)

            sessions = (
                LiveSession.objects
                .filter(
                    subject_id__in=subject_ids,
                    start_time__gte=today_start,
                    start_time__lt=today_end
                )
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            assignments = (
                Assignment.objects
                .filter(chapter_id__in=chapter_ids)
                .select_related("chapter__subject")
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
        # 👨‍🏫 TEACHER DASHBOARD (FIXED)
        # =========================
        else:

            today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)

            # ✅ Live sessions (today only — for "Upcoming Live Sessions")
            sessions = (
                LiveSession.objects
                .filter(
                    created_by=user,
                    start_time__gte=today_start,
                    start_time__lt=today_end
                )
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            # ✅ All upcoming sessions (for calendar & schedule)
            all_sessions = (
                LiveSession.objects
                .filter(
                    created_by=user,
                    start_time__gte=today_start
                )
                .select_related("subject", "created_by")
                .order_by("start_time")
            )

            # ✅ Assignments (FIXED RELATION)
            assignments = (
                Assignment.objects
                .filter(
                    chapter__subject__subject_teachers__teacher=user
                )
                .select_related("chapter__subject")
                .distinct()
                .order_by("due_date")
            )

            # ✅ Quizzes (usually has created_by)
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

        # ✅ Private sessions (upcoming, approved/pending)
        from django.db.models import Q
        private_sessions = (
            PrivateSession.objects
            .filter(
                Q(teacher=user) | Q(requested_by=user),
                scheduled_date__gte=timezone.now().date(),
                status__in=["pending", "approved", "needs_reconfirmation"]
            )
            .select_related("teacher", "requested_by")
            .order_by("scheduled_date", "scheduled_time")
        )

        notifications = (
            Activity.objects
            .filter(user=user)
            .order_by("-created_at")[:10]
        )

        schedule = (
            Activity.objects
            .filter(user=user)
            .order_by("due_date")[:10]
        )

        return Response({
            "sessions": DashboardSessionSerializer(sessions, many=True).data,
            "all_sessions": DashboardSessionSerializer(
                all_sessions if not is_student else sessions, many=True
            ).data,
            "assignments": DashboardAssignmentSerializer(assignments, many=True).data,
            "quizzes": DashboardQuizSerializer(quizzes, many=True).data,
            "private_sessions": DashboardPrivateSessionSerializer(private_sessions, many=True).data,
            "notifications": DashboardActivitySerializer(notifications, many=True).data,
            "schedule": DashboardActivitySerializer(schedule, many=True).data
        })
