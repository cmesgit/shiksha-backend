"""Domain services for enrollments.

Everything that mutates enrollment / subscription state should go through here
so the policy is in one place. Views and serializers call these helpers; they
never poke at the models directly.

NOTE: the per-course "free trial" feature was removed (the platform runs on the
free model — see enrollments/payments.py + global_settings). The trial helpers,
the `kind` field, and the trial reminder emails that used to live here are gone.
Subscriptions are now simply ACTIVE-until-expiry; access gating treats every
live subscription the same.
"""
from django.utils import timezone

from .models import Enrollment, Subscription


# =====================================================
# ACCESS CHECKS
# =====================================================

def is_user_enrolled(*, user, course) -> bool:
    """Legacy helper — kept for callers that only need the Enrollment row.

    For gating course CONTENT (videos, materials, livestream, quizzes), prefer
    ``has_active_subscription`` so an expired subscription blocks access.
    """
    return Enrollment.objects.filter(
        user=user,
        course=course,
        status=Enrollment.STATUS_ACTIVE,
    ).exists()


def has_active_subscription(*, user, course) -> bool:
    """True iff the user has any non-expired ACTIVE subscription for this course."""
    if not getattr(user, "is_authenticated", False):
        return False
    return Subscription.objects.filter(
        user=user,
        course=course,
        status=Subscription.STATUS_ACTIVE,
        expires_at__gt=timezone.now(),
    ).exists()


def get_active_subscription(*, user, course):
    """Return the user's currently-active subscription for this course, or None."""
    return (
        Subscription.objects
        .filter(
            user=user,
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        )
        .order_by("-expires_at")
        .first()
    )


def get_latest_subscription(*, user, course):
    """Return the user's most recent subscription for this course (any status),
    or None if they've never had one. Used to surface an expired one to the
    frontend (e.g. "expired on X" + renew CTA).
    """
    return (
        Subscription.objects
        .filter(user=user, course=course)
        .order_by("-expires_at")
        .first()
    )


# Access states returned by ``course_access_state``.
ACCESS_ACTIVE = "active"          # Has a live subscription — full content
ACCESS_EXPIRED = "expired"        # Had one, but expired — show locked snapshot
ACCESS_NONE = "none"              # Never enrolled / never had a subscription


def course_access_state(*, user, course) -> str:
    """Single source of truth for "what should this user see for this course".

    Returns one of:
      - ``"active"``  → serve full content
      - ``"expired"`` → serve a locked snapshot (metadata, no media URLs)
      - ``"none"``    → 404 / "enroll" CTA depending on context
    """
    if not getattr(user, "is_authenticated", False):
        return ACCESS_NONE
    if get_active_subscription(user=user, course=course):
        return ACCESS_ACTIVE
    if Subscription.objects.filter(user=user, course=course).exists():
        return ACCESS_EXPIRED
    return ACCESS_NONE


def lock_payload(*, user, course) -> dict:
    """Structured response body for ACTION endpoints when the subscription
    has expired. Frontend uses this to render the renew CTA contextually.

    Returned with HTTP 402 Payment Required.
    """
    latest = get_latest_subscription(user=user, course=course)
    return {
        "detail": "Your subscription for this course has expired.",
        "lock_reason": "subscription_expired",
        "course_id": str(course.id),
        "expires_at": latest.expires_at if latest else None,
        "renew_url": f"/enroll/{course.id}",
    }
