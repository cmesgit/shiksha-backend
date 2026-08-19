# skills/tasks.py
#
# Auto-decline pending SkillSession requests past the design's 24h SLA
# (WORKFLOW.md §2/§3: "Requests expire in 24h — auto-decline + refund").
# Mirrors livestream.tasks's sweep-task shape (a periodic Celery task that
# advances a status on a timer rather than only on read).

from django.utils import timezone

from config.celery import app
from .models import SkillSession
from .notifications import push_skill_bell


@app.task
def auto_decline_stale_requests():
    """Flip any REQUESTED session older than 24h to AUTO_DECLINED and
    release its held slot. Runs on a schedule (see config/celery.py); never
    silently loses a request — every one ends up DECLINED, AUTO_DECLINED, or
    CONFIRMED, never stuck in REQUESTED indefinitely."""
    from .teacher_views import free_slot

    cutoff = timezone.now() - timezone.timedelta(hours=24)
    stale = SkillSession.objects.filter(
        status=SkillSession.STATUS_REQUESTED, created_at__lt=cutoff
    ).select_related("expert")

    count = 0
    for sess in stale:
        sess.status = SkillSession.STATUS_AUTO_DECLINED
        sess.save(update_fields=["status", "updated_at"])
        if sess.slot_key:
            free_slot(sess.expert, sess.slot_key)
        push_skill_bell(sess, "declined")
        count += 1
    return count


# Grace period after a session's scheduled END before we call it lapsed.
# Generous on purpose: a tutor and learner who start 20 minutes late and run
# over must never have the session yanked out from under them mid-call. The
# cost of waiting is a stale row for an extra hour; the cost of being hasty
# is ending a live session.
LAPSE_GRACE = timezone.timedelta(hours=2)


@app.task
def lapse_unheld_sessions():
    """Move CONFIRMED sessions whose slot has fully passed to LAPSED.

    A session only becomes COMPLETED when someone actually ends the LiveKit
    room (skills/livekit_views.py). Nothing closed out a session that simply
    never happened, so it stayed CONFIRMED indefinitely — and because the
    learner's Upcoming/Past split is status-based rather than time-based, a
    long-past session sat in "Upcoming" with an active Join button.

    Deliberately narrow:
      · CONFIRMED only. REQUESTED has its own 24h SLA sweep above;
        PENDING_PAYMENT/NEEDS_RECONFIRMATION are awaiting a human, not a
        clock, and stealing them would hide work someone still has to do.
      · scheduled_for must be set. A confirmed session with no slot is a
        data problem to surface, not one to bury under a terminal status.
      · The whole booked duration plus LAPSE_GRACE must have elapsed.

    Payment is settled directly between learner and expert with no platform
    intermediary, so there is deliberately no refund step here — unlike
    auto_decline_stale_requests, this task only records what happened.
    """
    from django.db.models import F, ExpressionWrapper, DateTimeField
    from datetime import timedelta as _td

    now = timezone.now()
    candidates = (
        SkillSession.objects
        .filter(status=SkillSession.STATUS_CONFIRMED, scheduled_for__isnull=False)
        .select_related("expert")
    )

    count = 0
    for sess in candidates:
        ends_at = sess.scheduled_for + _td(minutes=sess.duration_mins or 0)
        if now < ends_at + LAPSE_GRACE:
            continue
        sess.status = SkillSession.STATUS_LAPSED
        sess.save(update_fields=["status", "updated_at"])
        count += 1
    return count
