# PLACEMENT: backend/backend/chat/migrations/0002_block.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0002_block.py
#
# Creates the Block table that powers chat blocking. Depends on the chat
# initial migration and on the accounts migration that defines LearnerProfile /
# TeacherProfile (the same dependency chat.0001 used).
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_teacherprofile_teacher_password"),
        ("chat", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Block",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("blocker_kind", models.CharField(choices=[("LEARNER", "Learner profile"), ("TEACHER", "Teacher identity")], max_length=10)),
                ("blocked_kind", models.CharField(choices=[("LEARNER", "Learner profile"), ("TEACHER", "Teacher identity")], max_length=10)),
                ("pair_key", models.CharField(db_index=True, max_length=120, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("blocked_learner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_blocks_received", to="accounts.learnerprofile")),
                ("blocked_teacher", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_blocks_received", to="accounts.teacherprofile")),
                ("blocker_learner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_blocks_made", to="accounts.learnerprofile")),
                ("blocker_teacher", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_blocks_made", to="accounts.teacherprofile")),
            ],
        ),
        migrations.AddIndex(
            model_name="block",
            index=models.Index(fields=["blocker_learner"], name="chat_block_blocker_l_idx"),
        ),
        migrations.AddIndex(
            model_name="block",
            index=models.Index(fields=["blocker_teacher"], name="chat_block_blocker_t_idx"),
        ),
        migrations.AddIndex(
            model_name="block",
            index=models.Index(fields=["blocked_learner"], name="chat_block_blocked_l_idx"),
        ),
        migrations.AddIndex(
            model_name="block",
            index=models.Index(fields=["blocked_teacher"], name="chat_block_blocked_t_idx"),
        ),
    ]
