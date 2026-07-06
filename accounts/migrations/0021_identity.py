# PLACEMENT: backend/backend/accounts/migrations/0021_identity.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/accounts/migrations/0021_identity.py
#
# M1 (Phase 3 architecture §6/§25): the Identity registry table. Schema only
# — see 0022_populate_identity.py for the data migration that backfills one
# row per existing LearnerProfile/TeacherProfile.
import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounts", "0020_academy_rejection"),
    ]

    operations = [
        migrations.CreateModel(
            name="Identity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(
                    choices=[("L", "Learner profile"), ("T", "Teacher identity"),
                             ("C", "Counsellor identity"), ("R", "Recruiter identity"),
                             ("S", "System / bot identity")],
                    db_index=True, max_length=1,
                )),
                ("profile_id", models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ("display_name", models.CharField(blank=True, max_length=150)),
                ("avatar_url", models.CharField(blank=True, max_length=500)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="identities", to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddIndex(
            model_name="identity",
            index=models.Index(fields=["account"], name="idx_identity_account"),
        ),
        migrations.AddConstraint(
            model_name="identity",
            constraint=models.UniqueConstraint(
                fields=["kind", "profile_id"], name="uniq_identity_per_profile"
            ),
        ),
    ]
