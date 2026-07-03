# PLACEMENT: backend/backend/notifications/migrations/0001_initial.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/migrations/0001_initial.py

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("verb", models.CharField(db_index=True, max_length=50)),
                ("title", models.CharField(max_length=255)),
                ("body", models.TextField(blank=True, default="")),
                ("link_url", models.CharField(blank=True, default="", max_length=500)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("audience_role", models.CharField(blank=True, choices=[("STUDENT", "Student"), ("TEACHER", "Teacher"), ("ADMIN", "Admin"), ("COUNSELOR", "Counselor")], default="", help_text="Optional. Restrict to one dashboard identity; blank = all.", max_length=20)),
                ("is_read", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="acted_notifications", to=settings.AUTH_USER_MODEL)),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "is_read"], name="notif_recipient_read_idx"),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "created_at"], name="notif_recipient_created_idx"),
        ),
    ]
