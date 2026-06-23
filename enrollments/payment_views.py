"""
enrollments/payment_views.py — additive endpoints for the pluggable payment
layer. Kept in a separate module so nothing in the existing views.py changes.

Routes (wired in enrollments/urls.py):
    GET  /api/enrollments/payment-config/   → active payment mode (any logged-in user)
    POST /api/enrollments/free-enroll/      → instant enrollment when mode is "free"
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from accounts.permissions import IsEmailVerified
from accounts.auth_flow import get_active_profile
from courses.models import Course

from .models import Enrollment, Subscription
from .payments import get_payment_provider


def _create_or_extend_subscription(*, user, learner, course):
    """Create (or extend) an active subscription for this learner+course.

    Defensive about the Subscription schema: only fields that actually exist on
    the model are passed, and `kind=PAID` is set only when the model defines it.
    This keeps the free path working regardless of trial-feature drift.
    """
    field_names = {f.name for f in Subscription._meta.get_fields()}
    days = getattr(course, "subscription_duration_days", None) or 30
    now = timezone.now()

    # Match on whatever owner field the model uses (learner_profile preferred).
    match = {"course": course, "status": Subscription.STATUS_ACTIVE, "expires_at__gt": now}
    if "learner_profile" in field_names and learner is not None:
        match["learner_profile"] = learner
    else:
        match["user"] = user

    active = Subscription.objects.filter(**match).order_by("-expires_at").first()
    if active:
        active.expires_at = active.expires_at + timedelta(days=days)
        active.save(update_fields=["expires_at"] + (["updated_at"] if "updated_at" in field_names else []))
        return active

    kwargs = {
        "user": user,
        "learner_profile": learner,
        "course": course,
        "starts_at": now,
        "expires_at": now + timedelta(days=days),
        "status": Subscription.STATUS_ACTIVE,
    }
    # Mark it PAID-equivalent when the model tracks a kind (free grant = full access).
    kind_paid = getattr(Subscription, "KIND_PAID", None)
    if "kind" in field_names and kind_paid is not None:
        kwargs["kind"] = kind_paid

    kwargs = {k: v for k, v in kwargs.items() if k in field_names}
    return Subscription.objects.create(**kwargs)


class PaymentConfigView(APIView):
    """What payment mode is live right now. Frontends use this to decide
    whether to show the UPI form, a gateway button, or a one-tap free enroll."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(get_payment_provider().describe())


class FreeEnrollView(APIView):
    """Instant enrollment while the platform is free. Refuses unless the active
    provider auto-activates, so flipping to a paid provider closes this door."""
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        provider = get_payment_provider()
        if not provider.auto_activate:
            return Response(
                {"detail": "This course requires payment.", "provider": provider.name},
                status=status.HTTP_400_BAD_REQUEST,
            )

        course_id = request.data.get("course")
        if not course_id:
            return Response({"course": "This field is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            course = Course.objects.get(pk=course_id)
        except Course.DoesNotExist:
            return Response({"detail": "Course not found."},
                            status=status.HTTP_404_NOT_FOUND)

        learner = get_active_profile(request)
        if learner is None:
            return Response({"detail": "Select a learner profile before enrolling."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Grant access (idempotent on the unique (learner_profile, course) pair).
        enroll_defaults = {"user": request.user, "status": Enrollment.STATUS_ACTIVE}
        Enrollment.objects.get_or_create(
            learner_profile=learner, course=course, defaults=enroll_defaults
        )
        sub = _create_or_extend_subscription(user=request.user, learner=learner, course=course)

        return Response(
            {
                "detail": "You're enrolled.",
                "course_id": str(course.id),
                "subscription": {
                    "id": str(sub.id),
                    "status": sub.status,
                    "expires_at": sub.expires_at,
                },
            },
            status=status.HTTP_201_CREATED,
        )
