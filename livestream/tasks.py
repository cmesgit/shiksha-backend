# PLACEMENT: backend/backend/livestream/tasks.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/livestream/tasks.py
#
# SUPERSEDES the set-1 tasks.py. Includes everything from set 1
# (notification group → user_updates_<id>, duplicate task removed) PLUS:
#
# NEW: sync_open_session_statuses() — a 1-minute sweep that calls
# session.sync_status() on every non-terminal session, so the reconnection
# ladder (RECONNECTING → PAUSED → COMPLETED) advances on a TIMER and the stored
# `status` column stops drifting from what computed_status() displays. It
# broadcasts only on a real transition. Wire it into beat at */1 min.
#
# The old auto_complete_expired_sessions is kept (end_time / abandoned cleanup)
# but now delegates the actual flip to sync_status() so the two never disagree
# on HOW a session becomes COMPLETED.

from config.celery import app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


@app.task(bind=True, max_retries=3, default_retry_delay=5)
def push_ws_notification_task(self, user_id, data):
    """Async WS notification push. Targets user_updates_<id> (the group
    accounts.consumers.UserUpdateConsumer joins). Retries up to 3×."""
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f"user_updates_{user_id}",
            {"type": "send_notification", "data": data},
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task
def sync_open_session_statuses():
    """Advance the live-session state machine on a timer.

    For every non-terminal LiveSession, persist computed_status() to the stored
    column via sync_status(). This makes the reconnection ladder
    (RECONNECTING→PAUSED→COMPLETED) fire at the real time boundary instead of
    only when someone reads the session, and keeps raw `status=` filters in
    sync with the displayed status. Broadcasts only on an actual change.

    Schedule (config/celery.py beat):
        "sync-open-session-statuses": {
            "task": "livestream.tasks.sync_open_session_statuses",
            "schedule": crontab(minute="*/1"),
        },
    """
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    open_qs = LiveSession.objects.exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    changed = 0
    for session in open_qs:
        did_change, _ = session.sync_status(save=True)
        if did_change:
            changed += 1
            try:
                broadcast_session_update(session)
            except Exception:
                pass

    return {"transitioned": changed}


@app.task
def auto_complete_expired_sessions():
    """Safety-net cleanup (every 5 min): close sessions past end_time or with a
    teacher gone > 60 min. Delegates the flip to sync_status() so the ladder
    and this task agree on how a session reaches COMPLETED.
    """
    from django.utils import timezone
    from datetime import timedelta
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    now = timezone.now()
    completed_count = 0

    candidates = LiveSession.objects.filter(
        end_time__lte=now,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )
    abandoned_cutoff = now - timedelta(minutes=60)
    abandoned = LiveSession.objects.filter(
        teacher_left_at__lte=abandoned_cutoff,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    seen = set()
    for session in list(candidates) + list(abandoned):
        if session.pk in seen:
            continue
        seen.add(session.pk)
        # end_time / abandoned both resolve to COMPLETED via computed_status();
        # sync_status persists it and clears the reconnect timer.
        did_change, new_status = session.sync_status(save=True)
        if new_status == LiveSession.STATUS_COMPLETED:
            # Force-complete the abandoned case even if computed_status hasn't
            # crossed its own 60-min boundary yet (defensive).
            if session.status != LiveSession.STATUS_COMPLETED:
                session.status = LiveSession.STATUS_COMPLETED
                session.teacher_left_at = None
                session.save(update_fields=["status", "teacher_left_at"])
                did_change = True
        if did_change:
            completed_count += 1
            try:
                broadcast_session_update(session)
            except Exception:
                pass

    return f"Auto-completed/advanced {completed_count} sessions"
