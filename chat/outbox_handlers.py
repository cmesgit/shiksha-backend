# PLACEMENT: backend/backend/chat/outbox_handlers.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/outbox_handlers.py
"""
chat/outbox_handlers.py — drains chat.models.OutboxEvent (Phase 3 §11).

drain_once() is called from chat/tasks.py's relay_outbox_task, itself on a
~10s Celery-beat schedule (config/celery.py — a float schedule, since
crontab's floor is 1 minute). Nothing here assumes it's ONLY ever called
from that task: it's a plain, synchronous, idempotent-enough function,
which is deliberate — it means a test (or an ops shell, if a queue needs a
manual kick) can call drain_once() directly with no Celery/worker
involved at all.

DELIVERY SEMANTICS: at-least-once, not exactly-once (see OutboxEvent's
docstring in chat/models.py). Each row is claimed with select_for_update()
inside its own short transaction so two overlapping drains (a slow beat
tick plus a manual one, say) don't both act on the same row, but that's a
courtesy against redundant work, not a correctness requirement — a
duplicate notify() call on a row that gets processed twice is an
acceptable cost. A row is only ever picked up while
attempts < OutboxEvent.MAX_ATTEMPTS; once a row hits that ceiling it stops
being selected at all (still inspectable via its `attempts`/`last_error`
for ops), which is the "bound retries via a per-row attempts counter" this
stage asked for.
"""
import logging

from django.db import transaction
from django.utils import timezone

from .models import OutboxEvent, Message, Conversation

logger = logging.getLogger(__name__)


def drain_once(batch_size=100):
    """Process up to `batch_size` pending events. Returns
    {"processed": n, "failed": n, "skipped": n} — used by tests and
    available for logging from the calling task."""
    counts = {"processed": 0, "failed": 0, "skipped": 0}
    event_ids = list(
        OutboxEvent.objects
        .filter(processed_at__isnull=True, attempts__lt=OutboxEvent.MAX_ATTEMPTS)
        .order_by("created_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    for event_id in event_ids:
        counts[_process_one(event_id)] += 1
    return counts


def _process_one(event_id):
    """Claims and processes exactly one row inside its own transaction, so
    a failure on row N doesn't roll back rows already committed as
    processed earlier in the same drain_once() batch."""
    with transaction.atomic():
        try:
            event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        except OutboxEvent.DoesNotExist:
            return "skipped"
        if event.processed_at is not None:
            return "skipped"  # a concurrent drain already handled it

        try:
            _handle_event(event)
        except Exception as exc:
            event.attempts += 1
            event.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            event.save(update_fields=["attempts", "last_error"])
            logger.exception(
                "chat.outbox_handlers: event %s (%s) failed — attempt %s/%s",
                event.pk, event.event_type, event.attempts, OutboxEvent.MAX_ATTEMPTS,
            )
            return "failed"

        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
        return "processed"


def _handle_event(event):
    if event.event_type == OutboxEvent.EVENT_MESSAGE_CREATED:
        _handle_message_created(event.payload)
    else:
        # Not raised: an unrecognized event_type is a code/data mismatch,
        # not a transient failure — retrying it changes nothing. Log and
        # move on rather than burning through this row's attempts budget.
        logger.warning(
            "chat.outbox_handlers: unknown event_type %r on event %s",
            event.event_type, event.pk,
        )


def _handle_message_created(payload):
    """Turns one chat.message_created event into a notifications.services
    .notify() call per OTHER participant, carrying M2's audience_identity
    (chat.Participant.identity_key() — the exact "L:<uuid>"/"T:<id>"
    format accounts.Identity.key and Notification.audience_identity both
    already use), which is what makes an offline sibling profile NOT see
    a notification meant for their sibling on the same account.
    """
    conversation_id = payload.get("conversation_id")
    message_id = payload.get("message_id")
    if not conversation_id or not message_id:
        raise ValueError(f"chat.message_created payload missing ids: {payload!r}")

    msg = (
        Message.objects
        .select_related("conversation", "sender")
        .filter(pk=message_id, conversation_id=conversation_id)
        .first()
    )
    if msg is None:
        # The message (or its conversation) is gone by the time we drained
        # this row. Nothing to notify about, and this isn't a transient
        # condition a retry would fix — so this is a no-op, not a failure.
        logger.info(
            "chat.outbox_handlers: message %s in conversation %s no longer "
            "exists — nothing to notify", message_id, conversation_id,
        )
        return

    conversation = msg.conversation
    sender_name = msg.sender.display_name() if msg.sender else "Unknown"
    preview = msg.body[:140]

    # Stage D (CC-015/CC-022): a message's verb depends on which kind of
    # conversation it landed in — BROADCAST is an Announcement, SUPPORT is
    # a ticket reply, everything else is an ordinary chat message. Each has
    # its own notifications/policy.py row (see that module) so preferences
    # and channel routing can differ per kind.
    if conversation.kind == Conversation.KIND_BROADCAST:
        verb = "announcement.posted"
    elif conversation.kind == Conversation.KIND_SUPPORT:
        verb = "support.reply"
    else:
        verb = "chat.message"

    # Lazy import: same cross-app-boundary discipline chat/services.py
    # already uses for courses/enrollments/skills — notifications doesn't
    # import chat back, so this isn't load-bearing against a real cycle,
    # just consistency with how every other cross-app call in this app is
    # written.
    from notifications.services import notify

    others = conversation.participants.exclude(pk=getattr(msg.sender, "pk", None))
    for participant in others.select_related("learner_profile", "teacher_profile", "staff_user"):
        # Mute scope is deliberately notifications-only: the unread counter
        # and inbox re-sort (chat/services.py's _fanout_new_message, which
        # runs earlier in post_message() — not touched here) stay live even
        # while muted. Muting silences push/email/SMS, it doesn't hide the
        # thread. `muted_until` is already loaded via the select_related
        # above — no extra query.
        if participant.muted_until and participant.muted_until > timezone.now():
            continue
        recipient = participant.account()
        if recipient is None:
            logger.warning(
                "chat.outbox_handlers: participant %s has no resolvable "
                "account — skipping notify", participant.pk,
            )
            continue
        title = f"New message from {sender_name}"
        if verb == "announcement.posted":
            title = f"New announcement from {sender_name}"
        elif verb == "support.reply":
            title = f"New reply from {sender_name} on your support ticket"

        notify(
            recipient=recipient,
            verb=verb,
            title=title,
            body=preview,
            link_url=f"/chat/{conversation.id}",
            payload={
                "conversation_id": str(conversation.id),
                "message_id": str(msg.id),
            },
            audience_identity=participant.identity_key(),
            # Legacy frame keys for the currently-deployed bells (mirrors
            # forum/views.py's identical shim) — the dashboards' bell
            # currently reads activity.Activity for its persisted list and
            # only recognizes a handful of `type` values for icon/color on
            # a LIVE push; this at least gives one instead of falling back
            # to a mislabeled "SESSION". Drop once the frontends read
            # notifications.Notification directly (see this stage's
            # Communication Center closure report for the full note).
            ws_extra={
                "type": "chat",
                "notification_type": verb,
                "message": title,
                "conversation_id": str(conversation.id),
            },
        )
