# PLACEMENT: backend/backend/chat/realtime.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/realtime.py
#
# Pushes an "inbox_delta" frame — {conversation_id, unread, preview,
# last_message_at, audience, learner_profile_id} — to one account's always-on
# ws/updates/ socket, so an inbox list re-sorts / re-badges live without a
# refetch. Mirrors livestream.services.notifications.push_ws_notification:
# prefer Celery, fall back to a synchronous channel-layer send if Celery is
# unavailable. Never raises — called from inside the message-send path, so a
# failure here must never turn into a lost or 500'd chat message.

import logging

logger = logging.getLogger(__name__)


def _send_sync(user_id, data):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"user_updates_{user_id}",
            {"type": "inbox_delta", "data": data},
        )


def push_inbox_delta(user_id, data):
    """Safe to call from anywhere, including inside a DB transaction — never
    breaks the caller if delivery fails.

    Uses apply_async(ignore_result=True), NOT .delay(). Discovered via M0
    testing: with a Redis result backend, .delay() sets up a result-consumer
    subscription on task submission — if Redis is unreachable, THAT retries
    internally for ~19s before this function's except clause ever runs,
    stalling the message-send path during exactly the outage fail-open is
    supposed to survive. We never read this task's result, so
    ignore_result=True skips that subscription entirely and fails fast.
    """
    try:
        from .tasks import push_inbox_delta_task
        push_inbox_delta_task.apply_async(args=(str(user_id), data), ignore_result=True)
    except Exception:
        try:
            _send_sync(user_id, data)
        except Exception:
            logger.exception("chat.realtime: inbox_delta push failed for user %s", user_id)
