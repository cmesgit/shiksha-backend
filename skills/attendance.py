"""Attendance capture for SkillSession (SkillDev 1-on-1 tutor sessions) —
append-only intervals + a derived per-user rollup.

Mirrors sessions_app/services/group_attendance.py exactly, operating on
SkillSessionAttendance / SkillSessionAttendanceInterval instead. Identity
scheme differs from group/private sessions: skills/livekit_views.py mints
"expert-{user.id}" / "learner-{user.id}" identities (not the composite
"{user.id}_{session.id}" scheme), and room names are "skill-session-{hex}".
"""
import uuid

from django.db import transaction
from django.utils import timezone

from .models import SkillSession
from .attendance_models import SkillSessionAttendance, SkillSessionAttendanceInterval

_ROOM_PREFIX = "skill-session-"


def resolve_skill_session(room_name):
    if not room_name or not room_name.startswith(_ROOM_PREFIX):
        return None
    try:
        session_id = uuid.UUID(room_name[len(_ROOM_PREFIX):])
    except ValueError:
        return None
    return SkillSession.objects.filter(id=session_id).first()


def parse_user_id(identity):
    """Recover the user id from the "expert-{id}" / "learner-{id}" LiveKit
    identity used for skill session rooms (see skills/livekit_views.py's
    _make_token). Returns None for an unrecognised identity shape."""
    if not identity:
        return None
    for prefix in ("expert-", "learner-"):
        if identity.startswith(prefix):
            return identity[len(prefix):]
    return None


@transaction.atomic
def open_interval(session, user, when=None):
    when = when or timezone.now()
    SkillSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=True)
    SkillSessionAttendanceInterval.objects.create(
        session=session, user=user, joined_at=when
    )
    _recompute_rollup(session, user)


@transaction.atomic
def close_intervals(session, user, when=None, reconcile=False):
    when = when or timezone.now()
    SkillSessionAttendanceInterval.objects.filter(
        session=session, user=user, left_at__isnull=True
    ).update(left_at=when, closed_by_reconcile=reconcile)
    _recompute_rollup(session, user)


@transaction.atomic
def close_all_open(session, when=None):
    when = when or timezone.now()
    open_qs = SkillSessionAttendanceInterval.objects.filter(
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
        SkillSessionAttendanceInterval.objects.filter(session=session, user_id=user_id)
    )
    if not intervals:
        return
    first_join = min(i.joined_at for i in intervals)
    closed = [i for i in intervals if i.left_at]
    last_left = max((i.left_at for i in closed), default=None)
    total = sum(i.duration_seconds() for i in intervals)
    SkillSessionAttendance.objects.update_or_create(
        session=session,
        user_id=user_id,
        defaults={
            "joined_at": first_join,
            "left_at": last_left,
            "total_seconds": total,
        },
    )
