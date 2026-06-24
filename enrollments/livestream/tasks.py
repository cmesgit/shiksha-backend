from config.celery import app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


@app.task(bind=True, max_retries=3, default_retry_delay=5)
def push_ws_notification_task(self, user_id, data):
    """
    Async Celery task to push WebSocket notification to a user.
    Retries up to 3 times if it fails.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f'notifications_{user_id}',
            {
                'type': 'send_notification',
                'data': data
            }
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task
def auto_complete_expired_sessions():
    """
    Runs every 5 minutes via Celery beat.
    Finds sessions past end_time and marks them COMPLETED.
    Also marks sessions where teacher left > 60 min ago as COMPLETED.
    """
    from django.utils import timezone
    from datetime import timedelta
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    now = timezone.now()
    completed_count = 0

    # Sessions past end_time
    expired = LiveSession.objects.filter(
        end_time__lte=now,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    for session in expired:
        session.status = LiveSession.STATUS_COMPLETED
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        try:
            broadcast_session_update(session)
        except Exception:
            pass
        completed_count += 1

    # Sessions where teacher left > 60 min ago
    abandoned_cutoff = now - timedelta(minutes=60)
    abandoned = LiveSession.objects.filter(
        teacher_left_at__lte=abandoned_cutoff,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    for session in abandoned:
        session.status = LiveSession.STATUS_COMPLETED
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        try:
            broadcast_session_update(session)
        except Exception:
            pass
        completed_count += 1

    return f"Auto-completed {completed_count} sessions"


@app.task
def auto_complete_expired_sessions():
    from django.utils import timezone
    from datetime import timedelta
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    now = timezone.now()
    completed_count = 0

    expired = LiveSession.objects.filter(
        end_time__lte=now,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    for session in expired:
        session.status = LiveSession.STATUS_COMPLETED
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        try:
            broadcast_session_update(session)
        except Exception:
            pass
        completed_count += 1

    abandoned_cutoff = now - timedelta(minutes=60)
    abandoned = LiveSession.objects.filter(
        teacher_left_at__lte=abandoned_cutoff,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    for session in abandoned:
        session.status = LiveSession.STATUS_COMPLETED
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        try:
            broadcast_session_update(session)
        except Exception:
            pass
        completed_count += 1

    return f"Auto-completed {completed_count} sessions"
