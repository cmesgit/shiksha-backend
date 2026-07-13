# PLACEMENT: backend/backend/chat/migrations/0008_communication_center_closure.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0008_communication_center_closure.py
#
# Schema for the Communication Center gap-analysis closure (Stages B/C/D):
#   - Participant: KIND_STAFF (+ staff_user) for admin/support-desk identities
#     (CC-022/023); pinned / archived_at / muted_until for CC-006 card actions.
#   - Message: message_type, reply_to (CC-007/010), soft delete via
#     deleted_at/deleted_by/deleted_reason (CC-006/010).
#   - MessageAttachment, MessageReaction — CC-010/012/016.
#   - Report — CC-006/010/023 (the admin moderation queue writes here).
#   - ChatSuspension — CC-023 "restrict user".
#   - CommPreference — CC-020/021 (per-identity online-status / read-receipt
#     toggles).
#   - SupportTicket — CC-022, wires the reserved SUPPORT conversation kind.
#   - Conversation: + unique_broadcast_per_context, the BROADCAST-kind sibling
#     of the existing unique_room_per_context (CC-015 Announcements).
#
# Generated against Django's autodetector then hand-trimmed: the raw diff
# also proposed renaming Block's four indexes
# (chat_block_blocker_l_idx → chat_block_blocker_71b343_idx, etc.). That
# rename is PRE-EXISTING drift between this Django version's index
# auto-naming and the names already baked into migration 0002_block.py — it
# reproduces identically even against an unmodified models.py (verified by
# generating this migration against the pre-Stage-B/C/D model file too), so
# it is not something this migration should touch. Left out on purpose; if
# you want it fixed, that is a separate, unrelated migration.
import chat.attachments
import chat.models
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0007_outbox_event"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── New models ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="ChatSuspension",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("identity_key", models.CharField(db_index=True, max_length=50, unique=True)),
                ("reason", models.CharField(blank=True, default="", max_length=255)),
                ("suspended_until", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="chat_suspensions_created", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="CommPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("identity_key", models.CharField(db_index=True, max_length=50, unique=True)),
                ("show_online_status", models.BooleanField(default=True)),
                ("show_read_receipts", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="Report",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("target_identity", models.CharField(blank=True, db_index=True, default="", max_length=50)),
                ("reason", models.CharField(choices=[("SPAM", "Spam or scam"), ("HARASSMENT", "Harassment or bullying"), ("INAPPROPRIATE", "Inappropriate content"), ("OTHER", "Something else")], max_length=20)),
                ("detail", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("REVIEWED", "Reviewed"), ("ACTION_TAKEN", "Action taken"), ("DISMISSED", "Dismissed")], default="OPEN", max_length=20)),
                ("resolution_note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reports", to="chat.conversation")),
                ("message", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reports", to="chat.message")),
                ("reporter", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reports_filed", to="chat.participant")),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="chat_reports_resolved", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SupportTicket",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("requester_kind", models.CharField(choices=[("LEARNER", "Learner profile"), ("TEACHER", "Teacher identity"), ("STAFF", "Support / admin staff")], max_length=10)),
                ("subject", models.CharField(max_length=200)),
                ("category", models.CharField(choices=[("TECHNICAL", "Technical issue"), ("BILLING", "Billing / payments"), ("COURSE", "Course content"), ("ACCOUNT", "Account / access"), ("OTHER", "Other")], default="OTHER", max_length=20)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("IN_PROGRESS", "In progress"), ("RESOLVED", "Resolved"), ("CLOSED", "Closed")], default="OPEN", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("assignee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_support_tickets", to=settings.AUTH_USER_MODEL)),
                ("conversation", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="support_ticket", to="chat.conversation")),
                ("requester_learner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="support_tickets", to="accounts.learnerprofile")),
                ("requester_teacher", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="support_tickets", to="accounts.teacherprofile")),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="MessageAttachment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("file", models.FileField(upload_to=chat.models.chat_attachment_upload_path, validators=[chat.attachments.validate_chat_attachment])),
                ("kind", models.CharField(choices=[("IMAGE", "Image"), ("PDF", "PDF"), ("DOCUMENT", "Document"), ("OTHER", "Other")], default="OTHER", max_length=10)),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=100)),
                ("size_bytes", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="chat.conversation")),
                ("message", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="attachment", to="chat.message")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_attachments", to="chat.participant")),
            ],
        ),
        migrations.AddIndex(
            model_name="messageattachment",
            index=models.Index(fields=["conversation", "created_at"], name="chat_messag_convers_809904_idx"),
        ),
        migrations.CreateModel(
            name="MessageReaction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("emoji", models.CharField(max_length=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reactions", to="chat.message")),
                ("participant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reactions_made", to="chat.participant")),
            ],
        ),
        migrations.AddIndex(
            model_name="messagereaction",
            index=models.Index(fields=["message"], name="chat_messag_message_4e17fc_idx"),
        ),
        migrations.AddConstraint(
            model_name="messagereaction",
            constraint=models.UniqueConstraint(fields=("message", "participant", "emoji"), name="unique_reaction_per_participant_emoji"),
        ),
        migrations.AddIndex(
            model_name="report",
            index=models.Index(fields=["status", "created_at"], name="chat_report_status_32cad0_idx"),
        ),
        migrations.AddIndex(
            model_name="report",
            index=models.Index(fields=["target_identity"], name="chat_report_target__baccab_idx"),
        ),
        migrations.AddIndex(
            model_name="supportticket",
            index=models.Index(fields=["status", "updated_at"], name="chat_suppor_status_755296_idx"),
        ),

        # ── Message: type / reply / soft delete ─────────────────────────
        migrations.AddField(
            model_name="message",
            name="message_type",
            field=models.CharField(choices=[("TEXT", "Text"), ("IMAGE", "Image attachment"), ("FILE", "File attachment"), ("SYSTEM", "System message"), ("ANNOUNCEMENT", "Announcement")], default="TEXT", max_length=12),
        ),
        migrations.AddField(
            model_name="message",
            name="reply_to",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replies", to="chat.message"),
        ),
        migrations.AddField(
            model_name="message",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="deleted_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deleted_messages", to="chat.participant"),
        ),
        migrations.AddField(
            model_name="message",
            name="deleted_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),

        # ── Participant: STAFF kind + CC-006 card actions ───────────────
        migrations.AddField(
            model_name="participant",
            name="staff_user",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_staff_participations", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="participant",
            name="kind",
            field=models.CharField(choices=[("LEARNER", "Learner profile"), ("TEACHER", "Teacher identity"), ("STAFF", "Support / admin staff")], max_length=10),
        ),
        migrations.AddField(
            model_name="participant",
            name="pinned",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="participant",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="participant",
            name="muted_until",
            field=models.DateTimeField(blank=True, help_text="Notifications for this thread are suppressed until this time. Null = not muted.", null=True),
        ),
        migrations.AddIndex(
            model_name="participant",
            index=models.Index(fields=["staff_user"], name="chat_partic_staff_u_a168c4_idx"),
        ),
        migrations.AddConstraint(
            model_name="participant",
            constraint=models.UniqueConstraint(condition=models.Q(("kind", "STAFF")), fields=("conversation", "staff_user"), name="unique_staff_per_conversation"),
        ),

        # ── Conversation: BROADCAST is unique per context too ───────────
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(condition=models.Q(("kind", "BROADCAST")), fields=("context_type", "context_id"), name="unique_broadcast_per_context"),
        ),
    ]
