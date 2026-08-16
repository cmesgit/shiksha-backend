"""Single source of truth for live-session limits and per-user entitlement.

Every room path — /live instant rooms, course classes, group sessions — reads
its limits through here so the admin panel (global_settings) is the only
place limits are set.

Adapted from the design handoff's reference implementation
(``design_handoff_live_sessions/backend/sessions_app/live_rules.py``) to this
repo's real names:
  * ``GroupSession`` has no ``scheduled_duration_minutes`` /
    ``actual_started_at`` / ``actual_ended_at`` — the real fields are
    ``duration_minutes``, ``room_started_at``, ``ended_at``.
  * There is no ``course.enrollments`` reverse accessor keyed on
    ``student``/``is_active``. The real ``Enrollment`` model (enrollments/
    models.py) is keyed on ``user`` + ``status == Enrollment.STATUS_ACTIVE``,
    dual-keyed with ``learner_profile__account`` exactly like the existing
    ``_guest_entitlement`` helper in ``group_session_views.py`` — mirrored
    here rather than guessed.
  * ``GroupSession`` has no direct ``course`` FK; the course is reached via
    ``session.subject.course`` (``subject`` is null for instant meetings).
  * There is no scalar ``user.role`` attribute. Role is RBAC-based:
    ``user.has_role("TEACHER")`` (see accounts/models.py, accounts/
    permissions.py, sessions_app/permissions.py's ``IsTeacher``).
  * ``user.has_active_subscription`` does not exist on the real User model.
    The real per-course entitlement equivalent is enrollments.models.
    Subscription (``status == ACTIVE`` and unexpired), scoped to the
    session's course — used here instead.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from global_settings.models import GlobalSettings


def rules():
    """The live-session limit row. Cached by GlobalSettings itself."""
    return GlobalSettings.load()


def features(s=None):
    s = s or rules()
    return {
        "recording": s.live_recording_enabled,
        "remote_access": s.live_remote_access_enabled,
        "chat": s.live_chat_enabled,
        "screenshare": s.live_screenshare_enabled,
        "show_tour": s.live_show_first_visit_tour,
    }


def limits(session=None, s=None):
    s = s or rules()
    return {
        "max_participants": s.live_max_participants,
        "cap_minutes": s.live_max_session_minutes,
        "free_minutes": s.live_free_minutes_per_join,
        "daily_minutes": s.live_daily_minutes_per_user,
        "extensions_allowed": s.live_host_extensions_allowed,
        "extension_minutes": s.live_host_extension_minutes,
        "max_upload_mb": s.live_max_upload_mb,
        "max_files": s.live_max_files_per_session,
        "file_retention_days": s.live_file_retention_days,
        "launch_free": s.live_launch_free_mode,
        "features": features(s),
    }


def minutes_used_today(user):
    """Sum of today's attendance for the daily budget."""
    from .models import GroupSessionAttendance

    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = GroupSessionAttendance.objects.filter(user=user, joined_at__gte=start)
    total = 0
    for r in rows:
        end = r.left_at or timezone.now()
        total += max(0, int((end - r.joined_at).total_seconds() // 60))
    return total


def _session_course(session):
    """The course this room belongs to, or None (instant meetings have no
    subject, hence no course)."""
    subject = getattr(session, "subject", None)
    return getattr(subject, "course", None) if subject else None


def is_enrolled_for(user, session):
    """True when the user's enrolment covers this session's course.

    Real ``Enrollment`` is keyed on ``user`` (not ``student``) with
    ``status == Enrollment.STATUS_ACTIVE`` (not a boolean ``is_active``), and
    dual-keyed against ``learner_profile__account`` — mirrors the existing
    ``_guest_entitlement`` check in ``group_session_views.py`` exactly, just
    scoped to this session's course instead of "any course".
    """
    from enrollments.models import Enrollment

    course = _session_course(session)
    if course is None:
        return False
    return (
        Enrollment.objects.filter(course=course, status=Enrollment.STATUS_ACTIVE)
        .filter(Q(user=user) | Q(learner_profile__account=user))
        .exists()
    )


def has_active_subscription_for(user, session):
    """Real equivalent of the doc's ``user.has_active_subscription`` (which
    doesn't exist on this repo's User model) — an unexpired active
    Subscription for this session's course."""
    from enrollments.models import Subscription

    course = _session_course(session)
    if course is None:
        return False
    return (
        Subscription.objects.filter(
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        )
        .filter(Q(user=user) | Q(learner_profile__account=user))
        .exists()
    )


def entitlement(user, session):
    """What this user is allowed in this room, right now.

    unlimited=True  -> no remaining_ms is sent, no countdown, no upsell
    unlimited=False -> free_minutes clock, then the ending-soon modal
    """
    s = rules()
    host_id = getattr(session, "host_id", None)

    if s.live_launch_free_mode:
        return {"unlimited": True, "reason": "launch_free"}
    if host_id and user.id == host_id:
        return {"unlimited": True, "reason": "host"}
    if user.has_role("TEACHER"):
        return {"unlimited": True, "reason": "teacher"}
    if is_enrolled_for(user, session):
        return {"unlimited": True, "reason": "enrolled"}
    if has_active_subscription_for(user, session):
        return {"unlimited": True, "reason": "subscribed"}

    used = minutes_used_today(user)
    if s.live_daily_minutes_per_user and used >= s.live_daily_minutes_per_user:
        return {
            "unlimited": False,
            "reason": "daily_exhausted",
            "free_minutes": 0,
            "minutes_used_today": used,
        }

    grant = s.live_free_minutes_per_join
    if s.live_daily_minutes_per_user:
        grant = min(grant, s.live_daily_minutes_per_user - used)
    return {
        "unlimited": False,
        "reason": "not_enrolled",
        "free_minutes": grant,
        "minutes_used_today": used,
    }


def can_host(user):
    s = rules()
    policy = s.live_host_policy
    if policy == "anyone":
        return True
    if user.has_role("TEACHER"):
        return True
    if policy == "teachers_only":
        return False
    from enrollments.models import Enrollment

    return user.enrollments.filter(status=Enrollment.STATUS_ACTIVE).exists()


def cap_ends_at(session):
    """Absolute end of the room: start + duration + granted extensions,
    capped at the admin ceiling.

    Real field names: ``room_started_at`` (not ``actual_started_at``),
    ``created_at``, ``duration_minutes`` (not ``scheduled_duration_minutes``).
    """
    s = rules()
    started = session.room_started_at or session.created_at
    minutes = min(
        (session.duration_minutes or s.live_max_session_minutes)
        + session.extensions_used * s.live_host_extension_minutes,
        s.live_max_session_minutes,
    )
    return started + timedelta(minutes=minutes)


def file_expires_at(session):
    """Real field name: ``ended_at`` (not ``actual_ended_at``)."""
    s = rules()
    base = session.ended_at or timezone.now()
    return base + timedelta(days=s.live_file_retention_days)
