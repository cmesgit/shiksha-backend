# PLACEMENT: backend/backend/livestream/services/notifications.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/livestream/services/notifications.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The old code broadcast to group `notifications_<user_id>` — a group no
# consumer ever joined, so every notification (teacher-went-live, new study
# material, …) was silently dropped. Both the Celery path and the synchronous
# fallback now target `user_updates_<user_id>`, the group joined by
# accounts.consumers.UserUpdateConsumer at ws/updates/. The event type stays
# `send_notification`, which that consumer now handles.

def _group(user_id):
    return f"user_updates_{user_id}"


def _send_sync(user_id, data):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            _group(user_id),
            {"type": "send_notification", "data": data},
        )


def push_ws_notification(user_id, data):
    """
    Push a real-time notification to a user via WebSocket.
    Prefers Celery for async processing; falls back to a synchronous
    channel-layer send if Celery is unavailable.
    Safe to call from anywhere — never breaks if it fails.

    Uses apply_async(ignore_result=True), NOT .delay(). With a Redis result
    backend, .delay() sets up a result-consumer subscription on submission;
    if Redis is unreachable THAT retries internally for ~19s before the
    except clause below ever runs — so every notify() call (this is the
    shared push path for the whole notifications system) would hang ~19s
    during a Redis outage instead of failing over to the sync send. We
    never read this task's result, so ignore_result=True skips the
    subscription and fails fast. (Same fix applied to chat.realtime in M0;
    flagged there as also needed here.)
    """
    try:
        from livestream.tasks import push_ws_notification_task
        push_ws_notification_task.apply_async(args=(str(user_id), data), ignore_result=True)
    except Exception:
        try:
            _send_sync(user_id, data)
        except Exception:
            pass
