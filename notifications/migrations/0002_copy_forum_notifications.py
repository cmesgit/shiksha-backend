# PLACEMENT: backend/backend/notifications/migrations/0002_copy_forum_notifications.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/migrations/0002_copy_forum_notifications.py
#
# Copies every row of forum.Notification into notifications.Notification,
# preserving read-state and original timestamps. Runs BEFORE the forum app
# drops its table (forum/0005 depends on this migration), so no ordering
# accident can lose data.
#
# Timestamp note: created_at is auto_now_add, which bulk_create would
# overwrite with "now". We insert first, then bulk_update created_at back
# to the original values (auto_now_add only fires on INSERT, so the
# update sticks).

from django.db import migrations

VERB_MAP = {
    "new_reply": "forum.reply",
    "upvote": "forum.upvote",
    "new_thread": "forum.thread",
}

BATCH = 500


def copy_forward(apps, schema_editor):
    OldNotification = apps.get_model("forum", "Notification")
    NewNotification = apps.get_model("notifications", "Notification")

    old_rows = (
        OldNotification.objects
        .select_related("thread")
        .order_by("created_at")
    )

    buffer = []  # list of (unsaved NewNotification, original created_at)
    for old in old_rows.iterator(chunk_size=BATCH):
        thread = old.thread
        payload = {
            "legacy_type": old.notification_type,
            "thread_id": old.thread_id,
        }
        if thread is not None:
            payload["title"] = thread.title

        buffer.append((
            NewNotification(
                recipient_id=old.recipient_id,
                actor_id=old.sender_id,
                verb=VERB_MAP.get(old.notification_type, "forum.event"),
                title=(old.message or "")[:255],
                body=old.message or "",
                link_url=f"/forum/thread/{old.thread_id}" if old.thread_id else "",
                payload=payload,
                is_read=old.is_read,
            ),
            old.created_at,
        ))

        if len(buffer) >= BATCH:
            _flush(NewNotification, buffer)
            buffer = []

    if buffer:
        _flush(NewNotification, buffer)


def _flush(NewNotification, buffer):
    """bulk_create, then restore original timestamps.

    bulk_create returns the SAME objects, in order, with pks set
    (PostgreSQL), so pairing by position is exact — no fuzzy matching.
    auto_now_add only fires on INSERT, so the follow-up bulk_update sticks.
    """
    objs = [pair[0] for pair in buffer]
    created = NewNotification.objects.bulk_create(objs)
    for obj, original_created_at in zip(created, (pair[1] for pair in buffer)):
        obj.created_at = original_created_at
    NewNotification.objects.bulk_update(created, ["created_at"], batch_size=BATCH)


def copy_backward(apps, schema_editor):
    """Reverse: drop only the rows this migration created (forum verbs
    carrying a legacy_type marker)."""
    NewNotification = apps.get_model("notifications", "Notification")
    NewNotification.objects.filter(
        verb__startswith="forum.",
        payload__has_key="legacy_type",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
        ("forum", "0004_merge_20260630_1651"),
    ]

    operations = [
        migrations.RunPython(copy_forward, copy_backward),
    ]
