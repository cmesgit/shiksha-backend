"""Domain services for enrollments.

Everything that mutates enrollment / subscription state should go through here
so the policy is in one place. Views and serializers call these helpers; they
never poke at the models directly.

ACCESS IS PER LEARNER PROFILE. Academy content is unlocked for the active
learner profile, not the account. The gate helpers therefore take an optional
``learner_profile`` and, when strict (the default), DENY access if no learner
profile is supplied (e.g. a teacher-context request). Callers resolve the
active profile with accounts.auth_flow.get_active_profile(request) and pass it.

During the migration window some legacy rows still have learner_profile=NULL;
run `manage.py backfill_profile_links` to populate them, then the strict gate
is fully correct.
"""
from django.db.models import Q
from django.utils import timezone

from .models import Enrollment, Subscription


# =====================================================
# ACCESS CHECKS
# =====================================================

def legacy_profile_q(learner_profile, *, field="learner_profile", user_field="user"):
    """Match rows for ``learner_profile``, including its legacy NULL-profile rows.

    Enrollments (and the Subscription/EnrollmentRequest rows hanging off them)
    created before the profile backfill carry ``learner_profile=NULL`` and are
    attributed to the account's DEFAULT profile — the same convention
    ``manage.py backfill_profile_links`` uses.

    This existed as an inline two-liner at six call sites and was MISSING at
    four others, which is worse than either extreme being applied uniformly:
    the queries that carried it succeeded and the ones that didn't returned
    nothing, so a legacy student got a dashboard that rendered but was
    permanently empty — zero sessions, zero assignments, zero quizzes, four
    stat cards at 0, forever. Two of the misses were also exploitable rather
    than merely empty: the catalog's ``is_enrolled`` said False for a course
    the student already held, and because Postgres treats NULLs as DISTINCT the
    ``unique_together`` did not stop the resulting second enrollment row.

    Guarding on ``is_default`` matters: without it a second child on the same
    account would inherit the parent account's legacy enrollments.

    ``field``/``user_field`` are for models that name these columns
    differently; the defaults suit Enrollment, Subscription and Assignment.
    """
    q = Q(**{field: learner_profile})
    if learner_profile is not None and getattr(learner_profile, "is_default", False):
        q |= Q(**{f"{field}__isnull": True, user_field: learner_profile.account})
    return q


def active_batch_id(*, learner_profile, course_id):
    """The batch this learner sits in for ``course_id``, or None.

    Content that is batch-scoped (materials, recordings, assignments) is shown
    as "course-wide (batch IS NULL) OR this learner's batch". Resolving that
    batch is the step that has to be PROFILE-scoped, not account-scoped: one
    account can hold two children enrolled in different batches of the same
    course, and an account-scoped ``.first()`` picks whichever row the database
    happens to return — so a sibling could be shown the other's batch content.

    Returns None when there's no active profile (e.g. a teacher-context
    request) or no matching enrollment. Callers treat None as "course-wide
    content only", which fails closed rather than leaking another batch's.
    """
    if learner_profile is None:
        return None
    enrollment = (
        Enrollment.objects
        .filter(
            learner_profile=learner_profile,
            course_id=course_id,
            status=Enrollment.STATUS_ACTIVE,
        )
        .only("batch_id")
        .first()
    )
    return enrollment.batch_id if enrollment else None


def is_user_enrolled(*, user, course, learner_profile=None) -> bool:
    """Legacy helper — kept for callers that only need the Enrollment row.

    When ``learner_profile`` is given, the enrollment must belong to it.
    For gating CONTENT prefer ``has_active_subscription``.
    """
    qs = Enrollment.objects.filter(course=course, status=Enrollment.STATUS_ACTIVE)
    if learner_profile is not None:
        qs = qs.filter(learner_profile=learner_profile)
    else:
        qs = qs.filter(user=user)
    return qs.exists()


def _subscription_qs(*, user, course, learner_profile, strict):
    """Base queryset, scoped to the learner profile when strict."""
    qs = Subscription.objects.filter(course=course)
    if learner_profile is not None:
        return qs.filter(learner_profile=learner_profile)
    if strict:
        # Strict but no active profile (teacher context / none selected) → deny.
        return Subscription.objects.none()
    return qs.filter(user=user)


def has_active_subscription(*, user, course, learner_profile=None, strict=True) -> bool:
    """True iff there's a non-expired ACTIVE subscription for this course,
    scoped to ``learner_profile`` when given.

    strict=True (default) + no learner_profile → False. This is what stops a
    teacher-context account from accessing academy content.
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


# Access states returned by ``course_access_state``.
ACCESS_ACTIVE = "active"          # Has a live subscription — full content
ACCESS_EXPIRED = "expired"        # Had one, but expired — show locked snapshot
ACCESS_NONE = "none"              # Never enrolled / never had a subscription


def course_access_state(*, user, course, learner_profile=None) -> str:
    """Single source of truth for "what should this user see for this course".

    Profile-scoped when learner_profile is given. Returns "active" | "expired"
    | "none".
    """
    if not getattr(user, "is_authenticated", False):
        return ACCESS_NONE
    if get_active_subscription(user=user, course=course, learner_profile=learner_profile):
        return ACCESS_ACTIVE
    historical = Subscription.objects.filter(course=course)
    if learner_profile is not None:
        historical = historical.filter(learner_profile=learner_profile)
    else:
        historical = historical.filter(user=user)
    if historical.exists():
        return ACCESS_EXPIRED
    return ACCESS_NONE


def lock_payload(*, user, course, learner_profile=None) -> dict:
    """Structured response body for ACTION endpoints when the subscription
    has expired / is missing for the active learner profile. HTTP 402.
    """
    latest = get_latest_subscription(user=user, course=course,
                                     learner_profile=learner_profile)
    return {
        "detail": "Your subscription for this course has expired.",
        "lock_reason": "subscription_expired",
        "course_id": str(course.id),
        "expires_at": latest.expires_at if latest else None,
        "renew_url": f"/enroll/{course.id}",
    }
