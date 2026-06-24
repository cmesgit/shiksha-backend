"""
REPLACEMENT for the ACCESS CHECKS block in enrollments/services.py
(from `def is_user_enrolled` down to `get_latest_subscription`, inclusive).

WHAT CHANGES & WHY
──────────────────
The old gate matched subscriptions on `user` only:

    Subscription.objects.filter(user=user, course=course, status=ACTIVE, ...)

That let a TEACHER-context request (no learner profile) pass as long as ANY
subscription row existed for the account — which is how a teacher-only account
ended up "subscribed". Academy learning is per LEARNER PROFILE, so the gate must
match the *active learner profile*, exactly like booking/chat already do via
get_active_profile().

DESIGN: backward-compatible signature.
  has_active_subscription(*, user, course, learner_profile=None, strict=True)

  • Pass learner_profile  → matches Subscription.learner_profile == that profile
    (the correct, strict behavior). If learner_profile is None and strict=True,
    access is DENIED (teacher context / no profile selected can't hold academy
    access).
  • strict=False           → legacy account-level match (user only). Only for
    transitional callers that genuinely can't resolve a profile yet.

Call sites pass `learner_profile=get_active_profile(request)`.
"""
from django.utils import timezone

from .models import Enrollment, Subscription


# =====================================================
# ACCESS CHECKS
# =====================================================

def is_user_enrolled(*, user, course, learner_profile=None) -> bool:
    """True iff there's an ACTIVE Enrollment for this course.

    When learner_profile is given, the enrollment must belong to that profile.
    Legacy helper — for gating CONTENT prefer has_active_subscription().
    """
    qs = Enrollment.objects.filter(course=course, status=Enrollment.STATUS_ACTIVE)
    if learner_profile is not None:
        qs = qs.filter(learner_profile=learner_profile)
    else:
        qs = qs.filter(user=user)
    return qs.exists()


def _subscription_qs(*, user, course, learner_profile, strict):
    """Shared base queryset for the gate, scoped by profile when strict."""
    qs = Subscription.objects.filter(course=course)
    if learner_profile is not None:
        # Profile-scoped: this exact learner profile's subscription.
        qs = qs.filter(learner_profile=learner_profile)
    elif strict:
        # Strict but no active profile (e.g. teacher context) → no access.
        return Subscription.objects.none()
    else:
        # Transitional/account-level fallback.
        qs = qs.filter(user=user)
    return qs


def has_active_subscription(*, user, course, learner_profile=None, strict=True) -> bool:
    """True iff there's a non-expired ACTIVE subscription for this course,
    scoped to `learner_profile` when given.

    strict=True (default) + no learner_profile → False (deny). This is the
    rule that stops a teacher-context account from accessing academy content.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    return (
        _subscription_qs(user=user, course=course,
                         learner_profile=learner_profile, strict=strict)
        .filter(status=Subscription.STATUS_ACTIVE, expires_at__gt=timezone.now())
        .exists()
    )


def get_active_subscription(*, user, course, learner_profile=None, strict=True):
    """Return the currently-active subscription for this course (profile-scoped
    when learner_profile is given), or None."""
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        _subscription_qs(user=user, course=course,
                         learner_profile=learner_profile, strict=strict)
        .filter(status=Subscription.STATUS_ACTIVE, expires_at__gt=timezone.now())
        .order_by("-expires_at")
        .first()
    )


def get_latest_subscription(*, user, course, learner_profile=None):
    """Most recent subscription for this course (any status), profile-scoped
    when learner_profile is given. Used to surface an expired one + renew CTA.
    """
    qs = Subscription.objects.filter(course=course)
    if learner_profile is not None:
        qs = qs.filter(learner_profile=learner_profile)
    else:
        qs = qs.filter(user=user)
    return qs.order_by("-expires_at").first()
