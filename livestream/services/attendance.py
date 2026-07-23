"""Attendance capture — append-only intervals + a derived per-user rollup.

Every join opens a new LiveSessionAttendanceInterval; every leave closes the
open one(s). LiveSessionAttendance is kept in sync as a rollup (first join,
last leave, total watch seconds) so existing readers keep working while
reconnect history stays fully recoverable in the interval rows.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from livestream.models import (
    LiveSessionAttendance,
    LiveSessionAttendanceInterval,
)


@transaction.atomic
def open_interval(session, user, when=None):
    """Record a join. Opens a fresh interval; closes any dangling open interval
    for this user first (defensive — a missed leave shouldn't leave two open)."""
    when = when or timezone.now()
    LiveSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=True)
    LiveSessionAttendanceInterval.objects.create(
        session=session, user=user, joined_at=when
    )
    _recompute_rollup(session, user)


@transaction.atomic
def close_intervals(session, user, when=None, reconcile=False):
    """Record a leave. Closes all currently-open intervals for this user."""
    when = when or timezone.now()
    LiveSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=reconcile)
    _recompute_rollup(session, user)


@transaction.atomic
def close_all_open(session, when=None):
    """room_finished / reconcile sweep: close every open interval in the session
    and refresh each affected user's rollup."""
    when = when or timezone.now()
    open_qs = LiveSessionAttendanceInterval.objects.filter(
        session=session, left_at__isnull=True
    )
    user_ids = list(open_qs.values_list("user_id", flat=True).distinct())
    open_qs.update(left_at=when, closed_by_reconcile=True)
    for uid in user_ids:
        _recompute_rollup_by_id(session, uid)


def _recompute_rollup(session, user):
    _recompute_rollup_by_id(session, user.id)


def _recompute_rollup_by_id(session, user_id):
    intervals = list(
        LiveSessionAttendanceInterval.objects.filter(session=session, user_id=user_id)
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
        defaults={
            "joined_at": first_join,
            "left_at": last_left,
            "total_seconds": total,
        },
    )


def current_watching(session):
    """Durable 'watching now' count = distinct users with an open interval."""
    return (
        LiveSessionAttendanceInterval.objects.filter(
            session=session, left_at__isnull=True
        )
        .values("user_id")
        .distinct()
        .count()
    )
