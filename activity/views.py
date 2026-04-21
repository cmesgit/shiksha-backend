# ============================================================
# BACKEND — activity/views.py  (FULL REPLACEMENT)
# ============================================================

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone

from .models import Activity
from .serializers import ActivitySerializer


class ActivityFeedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        now = timezone.now()

        # Only return relevant (non-stale) activities.
        # Sessions/assignments/quizzes with a past due_date are excluded
        # so the feed never shows expired items.
        qs = (
            Activity.objects
            .filter(user=request.user)
            .exclude(
                type__in=[
                    Activity.TYPE_SESSION,
                    Activity.TYPE_QUIZ,
                    Activity.TYPE_ASSIGNMENT,
                ],
                due_date__lt=now,
            )
            .order_by("-created_at")
        )

        # Optional type filter: ?type=ASSIGNMENT / QUIZ / SESSION / SUBMISSION
        activity_type = request.query_params.get("type")
        if activity_type:
            qs = qs.filter(type=activity_type)

        # Cursor pagination via ?limit= and ?offset=
        limit = min(int(request.query_params.get("limit", 20)), 50)
        offset = int(request.query_params.get("offset", 0))
        total = qs.count()
        activities = qs[offset: offset + limit]

        serializer = ActivitySerializer(activities, many=True)
        return Response({
            "results": serializer.data,
            "total": total,
            "limit": limit,
            "offset": offset,
        })


class MarkActivityReadView(APIView):
    """PATCH /activity/feed/<pk>/read/  — mark a single activity as read"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        updated = Activity.objects.filter(
            pk=pk, user=request.user).update(is_read=True)
        if not updated:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "ok"})


class MarkAllReadView(APIView):
    """POST /activity/feed/read-all/  — mark every activity as read for this user"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Activity.objects.filter(
            user=request.user, is_read=False).update(is_read=True)
        return Response({"status": "ok"})
