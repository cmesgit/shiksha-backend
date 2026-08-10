"""Attendance capture for 1-on-1 PrivateSessions — append-only intervals + a
derived per-user rollup.

Mirrors ``sessions_app/services/group_attendance.py`` exactly (which itself
mirrors ``livestream/services/attendance.py``), operating on
``PrivateSessionAttendance`` / ``PrivateSessionAttendanceInterval`` instead.
PrivateSession previously had no per-participant join/leave/duration
tracking at all — ``SessionParticipant.joined_at``/``left_at`` only cover
"additional students" in a group-format private session, not the primary
teacher/student pair, so this is a separate model rather than populating
those columns.
"""
from django.db import transaction
from django.utils import timezone

from sessions_app.models import (
    PrivateSession,
    PrivateSessionAttendance,
    PrivateSessionAttendanceInterval,
)


def resolve_private_session(room_name):
    return PrivateSession.objects.filter(room_name=room_name).first()


def parse_user_id(identity):
    """Recover the user id from the composite `"{user.id}_{session.id}"`
    LiveKit identity used for private session rooms (same scheme as group
    sessions — see private_token.py / group_session_token.py)."""
    if not identity:
        return None
    parts = identity.split("_", 1)
    return parts[0] if parts else None


@transaction.atomic
def open_interval(session, user, when=None):
    when = when or timezone.now()
    PrivateSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=True)
    PrivateSessionAttendanceInterval.objects.create(
        session=session, user=user, joined_at=when
    )
    _recompute_rollup(session, user)


@transaction.atomic
def close_intervals(session, user, when=None, reconcile=False):
    when = when or timezone.now()
    PrivateSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=reconcile)
    _recompute_rollup(session, user)


@transaction.atomic
def close_all_open(session, when=None):
    when = when or timezone.now()
    open_qs = PrivateSessionAttendanceInterval.objects.filter(
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
        PrivateSessionAttendanceInterval.objects.filter(session=session, user_id=user_id)
    )
    if not intervals:
        return
    first_join = min(i.joined_at for i in intervals)
    closed = [i for i in intervals if i.left_at]
    last_left = max((i.left_at for i in closed), default=None)
    total = sum(i.duration_seconds() for i in intervals)
    PrivateSessionAttendance.objects.update_or_create(
        session=session,
        user_id=user_id,
        defaults={
            "joined_at": first_join,
            "left_at": last_left,
            "total_seconds": total,
        },
    )
