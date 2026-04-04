import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PrivateSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("subject", models.CharField(max_length=255)),
                ("scheduled_date", models.DateField()),
                ("scheduled_time", models.TimeField()),
                ("duration_minutes", models.PositiveIntegerField(default=60)),
                ("rescheduled_date", models.DateField(blank=True, null=True)),
                ("rescheduled_time", models.TimeField(blank=True, null=True)),
                ("reschedule_reason", models.TextField(blank=True, default="")),
                ("session_type", models.CharField(choices=[("one_on_one", "One on One"), ("group", "Group")], default="one_on_one", max_length=20)),
                ("group_strength", models.PositiveIntegerField(default=1)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("declined", "Declined"), ("needs_reconfirmation", "Needs Reconfirmation"), ("ongoing", "Ongoing"), ("completed", "Completed"), ("cancelled", "Cancelled"), ("expired", "Expired"), ("withdrawn", "Withdrawn"), ("teacher_no_show", "Teacher No Show"), ("student_no_show", "Student No Show")], default="pending", max_length=30)),
                ("notes", models.TextField(blank=True, default="")),
                ("decline_reason", models.TextField(blank=True, default="")),
                ("cancel_reason", models.TextField(blank=True, default="")),
                ("room_name", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("teacher", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="taught_private_sessions", to=settings.AUTH_USER_MODEL)),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="requested_private_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["teacher", "status"], name="sessions_ap_teacher_idx"),
                    models.Index(fields=["requested_by", "status"], name="sessions_ap_request_idx"),
                    models.Index(fields=["status"], name="sessions_ap_status_idx"),
                    models.Index(fields=["scheduled_date"], name="sessions_ap_date_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SessionParticipant",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(choices=[("student", "Student"), ("observer", "Observer")], default="student", max_length=20)),
                ("joined_at", models.DateTimeField(blank=True, null=True)),
                ("left_at", models.DateTimeField(blank=True, null=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="participants", to="sessions_app.privatesession")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="private_session_participations", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "unique_together": {("session", "user")},
            },
        ),
        migrations.CreateModel(
            name="SessionRescheduleHistory",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("original_date", models.DateField()),
                ("original_time", models.TimeField()),
                ("proposed_date", models.DateField()),
                ("proposed_time", models.TimeField()),
                ("reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reschedule_history", to="sessions_app.privatesession")),
                ("proposed_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sender_name", models.CharField(max_length=255)),
                ("sender_role", models.CharField(default="student", max_length=20)),
                ("message", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chat_messages", to="sessions_app.privatesession")),
                ("sender", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="private_session_messages", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(fields=["session", "created_at"], name="sessions_ap_chat_idx"),
                ],
            },
        ),
    ]
