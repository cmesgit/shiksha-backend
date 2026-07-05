# PLACEMENT: backend/backend/chat/migrations/0004_participant_block_identity.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0004_participant_block_identity.py
#
# M1 (Phase 3 §6): nullable identity FKs on Participant and Block, dual-
# written alongside the existing polymorphic (kind, learner_profile,
# teacher_profile) columns. Schema only — see
# 0005_populate_identity_fk.py for the backfill.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0021_identity"),
        ("chat", "0003_message_client_id_dedupe"),
    ]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="identity",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="chat_participations_v2", to="accounts.identity",
            ),
        ),
        migrations.AddField(
            model_name="block",
            name="blocker_identity",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="identity_blocks_made", to="accounts.identity",
            ),
        ),
        migrations.AddField(
            model_name="block",
            name="blocked_identity",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="identity_blocks_received", to="accounts.identity",
            ),
        ),
    ]
