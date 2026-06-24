import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_teachercourseapplication_teacherskillapplication"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PasswordResetToken",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True)),
                ("code_hash", models.CharField(max_length=128)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("ticket", models.UUIDField(blank=True, null=True, unique=True)),
                ("ticket_expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="password_reset_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["user", "-created_at"], name="accounts_pa_user_id_created_idx"),
                    models.Index(fields=["ticket"], name="accounts_pa_ticket_idx"),
                ],
            },
        ),
    ]
