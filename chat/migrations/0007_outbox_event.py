# PLACEMENT: backend/backend/chat/migrations/0007_outbox_event.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0007_outbox_event.py
#
# M3 (Phase 3 §11): the transactional outbox table. Purely additive — no
# existing data to migrate, so unlike 0006 there's no ordering hazard here.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0006_context_generalization"),
    ]

    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
            fields=[
                ("id", models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name="ID",
                )),
                ("event_type", models.CharField(
                    choices=[("chat.message_created", "Message created")],
                    max_length=50,
                )),
                ("payload", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.TextField(blank=True, default="")),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["processed_at", "created_at"],
                        name="chat_outbox_process_480040_idx",
                    ),
                ],
            },
        ),
    ]
