# PLACEMENT: backend/backend/chat/migrations/0006_context_generalization.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0006_context_generalization.py
#
# M3 (Phase 3 §9): generalizes Conversation.course_id (UUIDField, COURSE-only)
# into (context_type, context_id) so a ROOM can be owned by any vertical.
# Old COURSE rows become ROOM rows with context_type="course".
#
# HAND-WRITTEN, NOT `makemigrations` output. On this codebase's Django
# version, `makemigrations` also proposes renaming four Block indexes
# (chat_block_blocker_l_idx -> chat_block_blocker_71b343_idx, and three
# siblings) with no underlying definition change — confirmed by running it
# and comparing; those four operations are deliberately NOT included here.
# They're unrelated pre-existing autodetector churn on a model this
# migration doesn't touch, not something M3 introduced.
#
# ORDERING — the exact bug that bit this stage before anything else was
# built on it (see chat/tests/test_migration_0006.py for the DB-backed
# proof this migration is required to pass):
#   1. ADD the new columns.
#   2. COPY data (RunPython) — course_id -> context_id, COURSE -> ROOM.
#   3. ONLY THEN drop the old constraint/index/column.
# An earlier draft of the copy function used `.save(update_fields=[...])`
# on the historical model, which failed with "NotUpdated: Save with
# update_fields did not affect any rows" — a query that silently touches
# zero rows, not an exception you're forced to notice. Rewritten below to
# use `.filter(pk=pk).update(...)`, which is what's actually verified to
# work. If `makemigrations` ever regenerates this migration, dropping
# course_id BEFORE the copy step would silently lose every course room's
# link to its course — this ordering is the whole point of hand-writing it.
from django.db import migrations, models


def copy_course_to_context(apps, schema_editor):
    """Forward: COURSE -> ROOM, course_id -> context_type="course"/context_id.

    `.filter(pk=pk).update(...)` — not `.save(update_fields=...)` — see the
    module docstring above for why that distinction is the entire point of
    this migration.
    """
    Conversation = apps.get_model("chat", "Conversation")
    for conv in (
        Conversation.objects.filter(kind="COURSE")
        .only("pk", "course_id")
        .iterator()
    ):
        Conversation.objects.filter(pk=conv.pk).update(
            kind="ROOM",
            context_type="course",
            context_id=str(conv.course_id) if conv.course_id else None,
        )


def copy_context_to_course(apps, schema_editor):
    """Backward: ROOM + context_type="course" -> COURSE + course_id.

    Reversible so a bad deploy can roll back without a manual data fixup.
    Runs after course_id has already been re-added (by this same
    migration's automatic reversal of RemoveField, later in the operations
    list below — Django resolves RunPython against the schema state at its
    OWN position in the list regardless of migration direction, so
    context_id/context_type are still present here to read from).
    """
    import uuid

    Conversation = apps.get_model("chat", "Conversation")
    for conv in (
        Conversation.objects.filter(kind="ROOM", context_type="course")
        .only("pk", "context_id")
        .iterator()
    ):
        course_uuid = None
        if conv.context_id:
            try:
                course_uuid = uuid.UUID(conv.context_id)
            except (ValueError, AttributeError, TypeError):
                course_uuid = None
        Conversation.objects.filter(pk=conv.pk).update(
            kind="COURSE", course_id=course_uuid,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0005_populate_identity_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="context_type",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="conversation",
            name="context_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="conversation",
            name="is_frozen",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            copy_course_to_context, copy_context_to_course,
        ),
        migrations.RemoveConstraint(
            model_name="conversation",
            name="unique_course_conversation",
        ),
        migrations.RemoveIndex(
            model_name="conversation",
            name="chat_conver_kind_145ccc_idx",
        ),
        migrations.RemoveField(
            model_name="conversation",
            name="course_id",
        ),
        migrations.AlterField(
            model_name="conversation",
            name="kind",
            field=models.CharField(
                choices=[
                    ("DIRECT", "Direct (1:1)"),
                    ("ROOM", "Group room"),
                    ("SESSION", "Session-scoped chat"),
                    ("SUPPORT", "Support thread"),
                    ("BROADCAST", "Broadcast (read-only)"),
                ],
                max_length=10,
            ),
        ),
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(
                fields=["kind", "context_type", "context_id"],
                name="chat_conver_kind_8a0932_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "ROOM")),
                fields=("context_type", "context_id"),
                name="unique_room_per_context",
            ),
        ),
    ]
