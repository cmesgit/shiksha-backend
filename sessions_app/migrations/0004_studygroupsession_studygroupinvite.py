# Generated for Study Groups feature.
#
# Adds StudyGroupSession and StudyGroupInvite alongside the existing
# private-session models without touching them.  The models mirror the
# PrivateSession "active_connections / all_left_at" pattern so the
# shared cleanup command and WebSocket consumer can cover both.

import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sessions_app", "0003_rename_sessions_ap_chat_idx_sessions_ap_session_f6dc4b_idx_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("courses", "0009_rename_updated_at_videoprogress_last_watched_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="StudyGroupSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("subject_name", models.CharField(max_length=255)),
                ("course_title", models.CharField(blank=True, default="", max_length=255)),
                ("topic", models.CharField(blank=True, default="", max_length=255)),
                ("scheduled_date", models.DateField()),
                ("scheduled_time", models.TimeField()),
                ("duration_minutes", models.PositiveIntegerField(
                    choices=[(30, "30 minutes"), (45, "45 minutes"), (60, "1 hour")],
                    default=45,
                )),
                ("max_invitees", models.PositiveIntegerField(default=20)),
                ("status", models.CharField(
                    choices=[
                        ("scheduled", "Scheduled"),
                        ("live", "Live"),
                        ("completed", "Completed"),
                        ("cancelled", "Cancelled"),
                        ("expired", "Expired"),
                    ],
                    default="scheduled",
                    max_length=20,
                )),
                ("cancel_reason", models.TextField(blank=True, default="")),
                ("room_name", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("room_started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("active_connections", models.IntegerField(default=0)),
                ("all_left_at", models.DateTimeField(blank=True, null=True)),
                ("host", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="hosted_study_groups",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("invited_teacher", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="invited_study_groups",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("subject", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="study_groups",
                    to="courses.subject",
                )),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="StudyGroupInvite",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("invite_role", models.CharField(
                    choices=[("student", "Student"), ("teacher", "Teacher")],
                    default="student",
                    max_length=10,
                )),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending"),
                        ("accepted", "Accepted"),
                        ("declined", "Declined"),
                    ],
                    default="pending",
                    max_length=10,
                )),
                ("decline_count", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("reinvited_at", models.DateTimeField(blank=True, null=True)),
                ("joined_at", models.DateTimeField(blank=True, null=True)),
                ("session", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="invites",
                    to="sessions_app.studygroupsession",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="study_group_invites",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddIndex(
            model_name="studygroupsession",
            index=models.Index(fields=["host", "status"], name="sessions_ap_host_sg_idx"),
        ),
        migrations.AddIndex(
            model_name="studygroupsession",
            index=models.Index(fields=["status"], name="sessions_ap_sg_status_idx"),
        ),
        migrations.AddIndex(
            model_name="studygroupsession",
            index=models.Index(fields=["scheduled_date"], name="sessions_ap_sg_date_idx"),
        ),
        migrations.AddIndex(
            model_name="studygroupinvite",
            index=models.Index(fields=["user", "status"], name="sessions_ap_inv_user_idx"),
        ),
        migrations.AddIndex(
            model_name="studygroupinvite",
            index=models.Index(fields=["session", "status"], name="sessions_ap_inv_sess_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="studygroupinvite",
            unique_together={("session", "user")},
        ),
    ]
