"""Domain services for enrollments.

Everything that mutates enrollment / subscription state should go through here
so the policy is in one place. Views and serializers call these helpers; they
never poke at the models directly.
"""
import logging
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.email_utils import send_gmail
from courses.models import Course

from .models import Enrollment, Subscription


logger = logging.getLogger(__name__)


# =====================================================
# ACCESS CHECKS
# =====================================================

def is_user_enrolled(*, user, course) -> bool:
    """Legacy helper — kept for callers that only need the Enrollment row.

    For gating course CONTENT (videos, materials, livestream, quizzes), prefer
    ``has_active_subscription`` so an expired trial blocks access correctly.
    """
    return Enrollment.objects.filter(
        user=user,
        course=course,
        status=Enrollment.STATUS_ACTIVE,
    ).exists()


def has_active_subscription(*, user, course) -> bool:
    """True iff the user has any non-expired ACTIVE subscription for this course.

    Treats TRIAL and PAID identically for access purposes — the kind only matters
    for billing/UX, not gating.
    """
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
    or None if they've never had one. Used to surface the expired one to the
    frontend so we can show "expired on X" + renew CTA.
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
      - ``"none"``    → 404 / "start trial" CTA depending on context

    LIST/DETAIL endpoints should branch on this value.
    ACTION endpoints should use ``HasActiveSubscription`` permission instead.
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


# =====================================================
# TRIAL ELIGIBILITY
# =====================================================

def has_used_trial(*, user, course) -> bool:
    """True iff a TRIAL subscription was ever issued to this user for this course.

    Existence is enough — even an EXPIRED/CANCELLED trial counts.
    A user only gets one trial per course, ever.
    """
    return Subscription.objects.filter(
        user=user,
        course=course,
        kind=Subscription.KIND_TRIAL,
    ).exists()


def trial_eligibility(*, user, course) -> dict:
    """Describe whether the user can start a trial right now.

    Returns a dict with shape:
        {
            "can_start": bool,
            "reason": str | None,            # set when can_start is False
            "has_used_trial": bool,
            "has_active_subscription": bool,
            "trial_duration_days": int,
        }
    """
    used = has_used_trial(user=user, course=course)
    active_sub = get_active_subscription(user=user, course=course)

    if used:
        return {
            "can_start": False,
            "reason": "You have already used your free trial for this course.",
            "has_used_trial": True,
            "has_active_subscription": bool(active_sub),
            "trial_duration_days": Subscription.TRIAL_DURATION_DAYS,
        }
    if active_sub:
        return {
            "can_start": False,
            "reason": "You already have active access to this course.",
            "has_used_trial": False,
            "has_active_subscription": True,
            "trial_duration_days": Subscription.TRIAL_DURATION_DAYS,
        }
    return {
        "can_start": True,
        "reason": None,
        "has_used_trial": False,
        "has_active_subscription": False,
        "trial_duration_days": Subscription.TRIAL_DURATION_DAYS,
    }


# =====================================================
# TRIAL START
# =====================================================

def start_trial(*, user, course):
    """Atomically grant a 30-day free trial.

    - Creates an ACTIVE Enrollment if one doesn't exist.
    - Creates an ACTIVE Subscription with kind=TRIAL.
    - Sends a "trial started" email (best-effort, swallows errors).

    Raises ``ValidationError`` if the user is not eligible.
    Returns the new Subscription.
    """
    with transaction.atomic():
        # Re-check eligibility inside the transaction; the unique constraint on
        # (user, course) where kind=TRIAL is the final safeguard against races.
        elig = trial_eligibility(user=user, course=course)
        if not elig["can_start"]:
            raise ValidationError({"detail": elig["reason"]})

        now = timezone.now()
        Enrollment.objects.get_or_create(
            user=user,
            course=course,
            defaults={"status": Enrollment.STATUS_ACTIVE},
        )

        subscription = Subscription.objects.create(
            user=user,
            course=course,
            kind=Subscription.KIND_TRIAL,
            starts_at=now,
            expires_at=now + timedelta(days=Subscription.TRIAL_DURATION_DAYS),
            status=Subscription.STATUS_ACTIVE,
        )

    _send_trial_started_email(subscription)
    return subscription


# =====================================================
# SUBSCRIPTION GRANT (used by EnrollmentRequest approval)
# =====================================================

def grant_paid_subscription(*, request_obj):
    """Create or extend a PAID Subscription tied to an approved EnrollmentRequest.

    Behavior:
    - If the user has an active PAID subscription → extend its expires_at.
    - If the user has an active TRIAL subscription → end it, then create a fresh
      PAID subscription starting at max(now, trial.expires_at) so the student
      doesn't lose any remaining trial time.
    - Otherwise → create a fresh PAID subscription starting now.

    Always returns the resulting PAID subscription.
    """
    course = request_obj.course
    days = course.subscription_duration_days or 30
    now = timezone.now()
    user = request_obj.user

    # Look at the user's currently-live subscription (could be trial or paid)
    active = (
        Subscription.objects
        .select_for_update()
        .filter(
            user=user,
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=now,
        )
        .order_by("-expires_at")
        .first()
    )

    if active and active.kind == Subscription.KIND_PAID:
        active.expires_at = active.expires_at + timedelta(days=days)
        active.save(update_fields=["expires_at", "updated_at"])
        return active

    # Trial-to-paid conversion: honor remaining trial days
    start_from = now
    if active and active.kind == Subscription.KIND_TRIAL:
        start_from = max(now, active.expires_at)
        active.status = Subscription.STATUS_EXPIRED
        active.save(update_fields=["status", "updated_at"])

    return Subscription.objects.create(
        user=user,
        course=course,
        kind=Subscription.KIND_PAID,
        starts_at=start_from,
        expires_at=start_from + timedelta(days=days),
        status=Subscription.STATUS_ACTIVE,
        source_request=request_obj,
    )


# =====================================================
# EMAILS
# =====================================================

def _student_app_url() -> str:
    return os.getenv("STUDENT_APP_URL", "https://app.shikshacom.com")


def _send_trial_started_email(subscription):
    user = subscription.user
    course_title = subscription.course.title
    end_date = subscription.expires_at.strftime("%B %d, %Y")
    days = Subscription.TRIAL_DURATION_DAYS
    app_url = _student_app_url()

    subject = f"Your {days}-day free trial of {course_title} has started"
    text = (
        f"Hi,\n\n"
        f"Your {days}-day free trial of \"{course_title}\" has started.\n"
        f"Your trial ends on {end_date}.\n\n"
        f"Start learning: {app_url}\n\n"
        f"— Shiksha Team"
    )
    html = f"""
    <h2>Your free trial has started</h2>
    <p>You now have {days} days of free access to <strong>{course_title}</strong>.</p>
    <p>Your trial ends on <strong>{end_date}</strong>.</p>
    <a href="{app_url}" style="padding:10px 15px;background:#16a34a;color:white;text-decoration:none;border-radius:5px;">
        Start learning
    </a>
    """
    try:
        send_gmail(to=user.email, subject=subject, message_text=text, html=html)
    except Exception as e:
        logger.error("Failed to send trial-started email to %s: %s", user.email, e)


def send_trial_reminder_email(subscription, *, days_left):
    """Trial nudge email at 7 days and 2 days before expiry."""
    user = subscription.user
    course_title = subscription.course.title
    end_date = subscription.expires_at.strftime("%B %d, %Y")
    app_url = _student_app_url()

    subject = f"{days_left} days left on your free trial — {course_title}"
    text = (
        f"Hi,\n\n"
        f"Your free trial of \"{course_title}\" ends on {end_date} ({days_left} days left).\n\n"
        f"To keep your access, enroll any time before then: {app_url}\n\n"
        f"— Shiksha Team"
    )
    html = f"""
    <h2>{days_left} days left on your free trial</h2>
    <p>Your free trial of <strong>{course_title}</strong> ends on <strong>{end_date}</strong>.</p>
    <p>Enroll any time before then to keep your access without interruption.</p>
    <a href="{app_url}" style="padding:10px 15px;background:#2563eb;color:white;text-decoration:none;border-radius:5px;">
        Continue with full access
    </a>
    """
    try:
        send_gmail(to=user.email, subject=subject, message_text=text, html=html)
    except Exception as e:
        logger.error("Failed to send trial-%dd reminder to %s: %s", days_left, user.email, e)


def send_trial_ended_email(subscription):
    """Final email when a trial transitions to EXPIRED."""
    user = subscription.user
    course_title = subscription.course.title
    app_url = _student_app_url()

    subject = f"Your free trial of {course_title} has ended"
    text = (
        f"Hi,\n\n"
        f"Your free trial of \"{course_title}\" has ended. "
        f"Enroll any time to regain access and keep learning.\n\n"
        f"{app_url}\n\n"
        f"— Shiksha Team"
    )
    html = f"""
    <h2>Your free trial has ended</h2>
    <p>Your free trial of <strong>{course_title}</strong> has ended.</p>
    <p>You can enroll any time to regain access.</p>
    <a href="{app_url}" style="padding:10px 15px;background:#dc2626;color:white;text-decoration:none;border-radius:5px;">
        Enroll now
    </a>
    """
    try:
        send_gmail(to=user.email, subject=subject, message_text=text, html=html)
    except Exception as e:
        logger.error("Failed to send trial-ended email to %s: %s", user.email, e)
