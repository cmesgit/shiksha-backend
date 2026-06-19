from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser

from accounts.permissions import IsAdmin, IsEmailVerified
from courses.models import Course

from .models import EnrollmentRequest, Enrollment
from .serializers import (
    EnrollmentRequestCreateSerializer,
    MyEnrollmentRequestSerializer,
    AdminEnrollmentRequestListSerializer,
    AdminActionSerializer,
    BatchStudentSerializer,
)
from .services import (
    start_trial,
    trial_eligibility,
    get_active_subscription,
)


# ---------- Student endpoints ----------

class EnrollmentRequestCreateView(generics.CreateAPIView):
    serializer_class = EnrollmentRequestCreateSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_serializer_context(self):
        return {"request": self.request}


class MyEnrollmentRequestListView(generics.ListAPIView):
    serializer_class = MyEnrollmentRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            EnrollmentRequest.objects
            .select_related("course")
            .filter(user=self.request.user)
        )


# ---------- Admin endpoints ----------

class AdminEnrollmentRequestListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            EnrollmentRequest.objects
            .select_related("user", "user__profile", "course")
            .order_by("-submitted_at")
        )

        status_filter = request.query_params.get("status", "").strip().upper()
        if status_filter in (
            EnrollmentRequest.STATUS_PENDING,
            EnrollmentRequest.STATUS_APPROVED,
            EnrollmentRequest.STATUS_REJECTED,
        ):
            qs = qs.filter(status=status_filter)

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(100, max(1, int(request.query_params.get("page_size", 50))))
        except (TypeError, ValueError):
            page_size = 50

        count = qs.count()
        start = (page - 1) * page_size
        results = qs[start:start + page_size]

        serializer = AdminEnrollmentRequestListSerializer(
            results, many=True, context={"request": request}
        )
        return Response({"count": count, "results": serializer.data})


class AdminEnrollmentRequestActionView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, request_id):
        try:
            req = EnrollmentRequest.objects.select_related("user", "course").get(
                pk=request_id
            )
        except EnrollmentRequest.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Pass request_obj so the serializer can validate batch ↔ course + capacity.
        serializer = AdminActionSerializer(
            data=request.data,
            context={"request": request, "request_obj": req},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(request_obj=req, reviewer=request.user)

        out = AdminEnrollmentRequestListSerializer(req, context={"request": request})
        return Response(out.data)


# ---------- Free trial ----------

def _get_course_or_404(course_id):
    try:
        return Course.objects.get(pk=course_id)
    except Course.DoesNotExist:
        raise NotFound("Course not found.")


def _subscription_snapshot(sub):
    if sub is None:
        return None
    return {
        "id": str(sub.id),
        "kind": sub.kind,
        "status": sub.status,
        "starts_at": sub.starts_at,
        "expires_at": sub.expires_at,
        "days_remaining": sub.days_remaining,
        "is_trial": sub.is_trial,
    }


class TrialStatusView(APIView):
    """GET /api/enrollments/courses/<course_id>/trial-status/

    Returns whether the user can start a free trial, plus the current
    subscription snapshot (if any). Used by the frontend to decide which
    CTA to show on the course detail page.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request, course_id):
        course = _get_course_or_404(course_id)
        elig = trial_eligibility(user=request.user, course=course)
        return Response({
            **elig,
            "subscription": _subscription_snapshot(
                get_active_subscription(user=request.user, course=course)
            ),
        })


class StartTrialView(APIView):
    """POST /api/enrollments/courses/<course_id>/start-trial/

    Atomically grants a 30-day free trial. Idempotent against races via the
    unique constraint on (user, course) where kind=TRIAL.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request, course_id):
        course = _get_course_or_404(course_id)
        subscription = start_trial(user=request.user, course=course)
        from .models import Subscription
        return Response(
            {
                "detail": f"Your {Subscription.TRIAL_DURATION_DAYS}-day free trial has started.",
                "subscription": _subscription_snapshot(subscription),
            },
            status=status.HTTP_201_CREATED,
        )


class AdminBatchRosterView(APIView):
    """List enrolled students, filterable by batch / course / status.

    Query params:
      - batch:   Batch UUID (exact)
      - code:    Batch code, e.g. "A13" (case-insensitive)
      - course:  Course UUID
      - status:  ACTIVE (default) / REVOKED
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            Enrollment.objects
            .select_related("user", "user__profile", "course")
            .order_by("batch_code", "user__email")
        )

        batch_id = request.query_params.get("batch")
        code = request.query_params.get("code", "").strip()
        course_id = request.query_params.get("course")
        status_filter = request.query_params.get("status", Enrollment.STATUS_ACTIVE).strip().upper()

        if batch_id:  # NOTE: no batch FK; filtering by batch_code only
            pass  # batch_id filter not supported (no batch FK on Enrollment)
        if code:
            qs = qs.filter(batch_code__iexact=code.replace(" ", ""))
        if course_id:
            qs = qs.filter(course_id=course_id)
        if status_filter in (Enrollment.STATUS_ACTIVE, Enrollment.STATUS_REVOKED):
            qs = qs.filter(status=status_filter)

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))
        except (TypeError, ValueError):
            page_size = 50

        count = qs.count()
        start = (page - 1) * page_size
        results = qs[start:start + page_size]

        serializer = BatchStudentSerializer(results, many=True, context={"request": request})
        return Response({"count": count, "results": serializer.data})
