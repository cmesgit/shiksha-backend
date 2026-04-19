from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from .models import Order


class AdminOrderSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source="user.email", read_only=True)
    course_title = serializers.CharField(source="course.title", read_only=True)

    class Meta:
        model = Order
        fields = (
            "id",
            "user_name",
            "user_email",
            "course_title",
            "amount",
            "status",
            "razorpay_order_id",
            "created_at",
        )

    def get_user_name(self, obj):
        profile = getattr(obj.user, "profile", None)
        if profile and profile.full_name:
            return profile.full_name
        return obj.user.username or obj.user.email


class AdminOrderListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            Order.objects
            .select_related("user", "user__profile", "course")
            .order_by("-created_at")
        )

        status_filter = request.query_params.get("status", "").strip().upper()
        if status_filter in (Order.STATUS_CREATED, Order.STATUS_PAID, Order.STATUS_FAILED):
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

        return Response({
            "count": count,
            "results": AdminOrderSerializer(results, many=True).data,
        })
