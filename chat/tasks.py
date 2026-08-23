# PLACEMENT: backend/backend/chat/tasks.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/tasks.py
#
# M3 (Phase 3 §11): adds relay_outbox_task, the Celery-beat entry point for
# draining chat.models.OutboxEvent (config/celery.py's "relay-chat-outbox"
# schedule). push_inbox_delta_task below is unchanged.
#
# Mirrors livestream.tasks.push_ws_notification_task: same target group
# (user_updates_<id>, the group accounts.consumers.UserUpdateConsumer joins),
# same retry policy. Kept as its own task (rather than reusing the livestream
# one) so chat's realtime volume never competes for retry slots with
# notification delivery, and so the chat app doesn't import livestream.
from config.celery import app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
import logging

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=5)
def push_inbox_delta_task(self, user_id, data):
    """Async inbox-delta push. Targets user_updates_<id>; the consumer's
    existing _wanted() identity filter (audience / learner_profile_id)
    applies unchanged — this task doesn't need to know about it."""
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f"user_updates_{user_id}",
            {"type": "inbox_delta", "data": data},
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task
def expire_old_attachments():
    """Beat-driven sweep for temporary file sharing: soft-deletes the message
    behind any MessageAttachment whose expires_at has passed. Reuses the same
    soft-delete-and-broadcast mechanism as an admin/moderator removal
    (participant=None is already a supported "system actor" call shape —
    see AdminResolveReportView) rather than a new mechanism. Per-row
    try/except so one bad row can't abort the whole sweep."""
    from django.utils import timezone
    from . import services, realtime
    from .models import MessageAttachment

    expired = 0
    qs = (
        MessageAttachment.objects
        .filter(expires_at__lte=timezone.now(), message__deleted_at__isnull=True)
        .select_related("message")
    )
    for attachment in qs:
        try:
            services.soft_delete_message(
                attachment.message, participant=None, admin_reason="expired",
            )
            realtime.push_conversation_event(
                attachment.conversation_id, "chat.message_deleted",
                {"id": str(attachment.message_id)},
            )
            expired += 1
        except Exception:
            logger.exception(
                "chat.tasks: expire_old_attachments failed for attachment %s",
                attachment.id,
            )
    return {"expired": expired}


@app.task
def relay_outbox_task():
    """Beat-driven drain of chat.models.OutboxEvent — see
    chat/outbox_handlers.py for the actual work. No bind/retry here: retry
    logic already lives per-row in the outbox itself (OutboxEvent.attempts),
    so a task-level retry would just be a second, redundant retry budget on
    top of that one. Logs the batch counts; never raises (a mid-batch
    exception would mean a bug in drain_once() itself, which the next
    beat tick will simply try again 10s later either way).
    """
    from . import outbox_handlers
    try:
        counts = outbox_handlers.drain_once()
        if counts["processed"] or counts["failed"]:
            logger.info("chat.tasks: outbox drain %s", counts)
    except Exception:
        logger.exception("chat.tasks: relay_outbox_task crashed")
