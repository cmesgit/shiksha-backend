"""Attendance capture for group sessions — append-only intervals + a derived
per-user rollup.

Mirrors ``livestream/services/attendance.py`` exactly, operating on
``GroupSessionAttendance`` / ``GroupSessionAttendanceInterval`` instead.
Every join opens a new GroupSessionAttendanceInterval; every leave closes the
open one(s). GroupSessionAttendance is kept in sync as a rollup (first join,
last leave, total watch seconds) so reconnect history stays fully recoverable
in the interval rows.
"""
from django.db import transaction
from django.utils import timezone

from sessions_app.models import (
    GroupSession,
    GroupSessionAttendance,
    GroupSessionAttendanceInterval,
    GroupSessionInvite,
)


def resolve_group_session(room_name):
    return GroupSession.objects.filter(room_name=room_name).first()


def parse_user_id(identity):
    """Recover the user id from the composite `"{user.id}_{session.id}"`
    LiveKit identity used for group session rooms. Returns None if identity
    is empty or doesn't contain the separator."""
    if not identity:
        return None
    parts = identity.split("_", 1)
    return parts[0] if parts else None


@transaction.atomic
def open_interval(session, user, when=None):
    """Record a join. Opens a fresh interval; closes any dangling open interval
    for this user first (defensive — a missed leave shouldn't leave two open)."""
    when = when or timezone.now()
    GroupSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=True)
    GroupSessionAttendanceInterval.objects.create(
        session=session, user=user, joined_at=when
    )
    _recompute_rollup(session, user)
    GroupSessionInvite.objects.filter(
        session=session, user=user, joined_at__isnull=True
    ).update(joined_at=when)


@transaction.atomic
def close_intervals(session, user, when=None, reconcile=False):
    """Record a leave. Closes all currently-open intervals for this user."""
    when = when or timezone.now()
    GroupSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=reconcile)
    _recompute_rollup(session, user)


@transaction.atomic
def close_all_open(session, when=None):
    """Reconcile sweep: close every open interval in the session and refresh
    each affected user's rollup."""
    when = when or timezone.now()
    open_qs = GroupSessionAttendanceInterval.objects.filter(
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
        GroupSessionAttendanceInterval.objects.filter(session=session, user_id=user_id)
    )
    if not intervals:
        return
    first_join = min(i.joined_at for i in intervals)
    closed = [i for i in intervals if i.left_at]
    last_left = max((i.left_at for i in closed), default=None)
    total = sum(i.duration_seconds() for i in intervals)
    GroupSessionAttendance.objects.update_or_create(
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
        GroupSessionAttendanceInterval.objects.filter(
            session=session, left_at__isnull=True
        )
        .values("user_id")
        .distinct()
        .count()
    )
