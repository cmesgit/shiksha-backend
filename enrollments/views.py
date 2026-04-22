from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser

from accounts.permissions import IsAdmin

from .models import EnrollmentRequest
from .serializers import (
    EnrollmentRequestCreateSerializer,
    MyEnrollmentRequestSerializer,
    AdminEnrollmentRequestListSerializer,
    AdminActionSerializer,
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

        serializer = AdminActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(request_obj=req, reviewer=request.user)

        out = AdminEnrollmentRequestListSerializer(req, context={"request": request})
        return Response(out.data)
