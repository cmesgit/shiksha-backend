"""Attendance capture — append-only intervals + a derived per-user rollup.

Every join opens a new LiveSessionAttendanceInterval; every leave closes the
open one(s). LiveSessionAttendance is kept in sync as a rollup (first join,
last leave, total watch seconds) so existing readers keep working while
reconnect history stays fully recoverable in the interval rows.

⚠️ SCOPED PER LEARNER PROFILE, not per account.

One email is one account holding many LearnerProfiles — a parent and their
children. Everything here used to key on `user` alone, so two siblings
attending different classes on the same account had their watch time summed
into a single row that then showed up as BOTH children's attendance. A parent
looking at either child's record saw the other's minutes.

`learner_profile=None` is legitimate and means "not a learner": teachers, and
rows written before this scoping existed. It is never a wildcard — a lookup
for profile A must not match a row for profile B or a NULL row.
"""
from django.db import transaction
from django.utils import timezone

from livestream.models import (
    LiveSessionAttendance,
    LiveSessionAttendanceInterval,
)


def _scope(qs, user, learner_profile):
    """Narrow to exactly one (user, profile) pair.

    learner_profile_id=None becomes an IS NULL match rather than "any", which
    is what keeps a teacher's row and a learner's row from colliding.
    """
    return qs.filter(
        user=user,
        learner_profile_id=getattr(learner_profile, "id", learner_profile),
    )


@transaction.atomic
def open_interval(session, user, when=None, learner_profile=None):
    """Record a join. Opens a fresh interval; closes any dangling open interval
    for this user+profile first (defensive — a missed leave shouldn't leave
    two open)."""
    when = when or timezone.now()
    _scope(
        LiveSessionAttendanceInterval.objects.filter(
            session=session, left_at__isnull=True),
        user, learner_profile,
    ).update(left_at=when, closed_by_reconcile=True)
    LiveSessionAttendanceInterval.objects.create(
        session=session, user=user, joined_at=when,
        learner_profile_id=getattr(learner_profile, "id", learner_profile),
    )
    _recompute_rollup_by_id(
        session, user.id,
        getattr(learner_profile, "id", learner_profile))


@transaction.atomic
def close_intervals(session, user, when=None, reconcile=False, learner_profile=None):
    """Record a leave. Closes all currently-open intervals for this user+profile."""
    when = when or timezone.now()
    _scope(
        LiveSessionAttendanceInterval.objects.filter(
            session=session, left_at__isnull=True),
        user, learner_profile,
    ).update(left_at=when, closed_by_reconcile=reconcile)
    _recompute_rollup_by_id(
        session, user.id,
        getattr(learner_profile, "id", learner_profile))


@transaction.atomic
def close_user(session, user, when=None, reconcile=False):
    """Close every open interval for this USER, whichever profile they used.

    For "this account is gone" events — being removed by the teacher — where
    the caller has a user but no profile. close_intervals() would match only
    the NULL-profile rows and silently leave a learner's real interval open,
    which then reads as 0 seconds on the roster.
    """
    when = when or timezone.now()
    if user is None:
        return
    open_qs = LiveSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True)
    profile_ids = list(open_qs.values_list("learner_profile_id", flat=True).distinct())
    open_qs.update(left_at=when, closed_by_reconcile=reconcile)
    for pid in profile_ids:
        _recompute_rollup_by_id(session, user.id, pid)


@transaction.atomic
def close_all_open(session, when=None):
    """room_finished / reconcile sweep: close every open interval in the session
    and refresh each affected (user, profile) rollup."""
    when = when or timezone.now()
    open_qs = LiveSessionAttendanceInterval.objects.filter(
        session=session, left_at__isnull=True
    )
    # Recompute per (user, profile), not per user — otherwise a parent account
    # with two children in the same class gets one rollup recomputed twice and
    # the other not at all.
    pairs = list(open_qs.values_list("user_id", "learner_profile_id").distinct())
    open_qs.update(left_at=when, closed_by_reconcile=True)
    for uid, pid in pairs:
        _recompute_rollup_by_id(session, uid, pid)


def _recompute_rollup(session, user, learner_profile=None):
    _recompute_rollup_by_id(
        session, user.id, getattr(learner_profile, "id", learner_profile))


def _recompute_rollup_by_id(session, user_id, learner_profile_id=None):
    intervals = list(
        LiveSessionAttendanceInterval.objects.filter(
            session=session, user_id=user_id,
            learner_profile_id=learner_profile_id,
        )
    )
    if not intervals:
        return
    first_join = min(i.joined_at for i in intervals)
    closed = [i for i in intervals if i.left_at]
    last_left = max((i.left_at for i in closed), default=None)
    total = sum(i.duration_seconds() for i in intervals)
    LiveSessionAttendance.objects.update_or_create(
        session=session,
        user_id=user_id,
        learner_profile_id=learner_profile_id,
        defaults={
            "joined_at": first_join,
            "left_at": last_left,
            "total_seconds": total,
        },
    )


def current_watching(session):
    """Durable 'watching now' count.

    Counts distinct (user, profile) pairs, not distinct users — two children
    of one parent watching the same class really are two attendees, and
    counting the account once under-reported the room.
    """
    return (
        LiveSessionAttendanceInterval.objects.filter(
            session=session, left_at__isnull=True
        )
        .values("user_id", "learner_profile_id")
        .distinct()
        .count()
    )
