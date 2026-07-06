# PLACEMENT: backend/backend/notifications/migrations/0003_audience_identity.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/migrations/0003_audience_identity.py
#
# M2 (Phase 3 §18): add Notification.audience_identity — the precise
# per-identity scope that closes the child-A/child-B leak. Schema only;
# existing rows keep audience_identity="" (account-wide), which preserves
# their current behaviour exactly. No data backfill is needed or correct
# here: an existing STUDENT-scoped row genuinely was account-wide-for-
# students under the old model, and we must not retroactively guess which
# child it "should" have been for.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_copy_forum_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="audience_identity",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=50,
                help_text=(
                    'Optional. Restrict to ONE identity, e.g. "L:<uuid>" / '
                    '"T:<id>" (accounts.Identity.key format). Blank = every '
                    "identity on the account. This is the precise per-profile "
                    "scope; audience_role is the coarse legacy fallback."
                ),
            ),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["recipient", "audience_identity"],
                name="idx_notif_recipient_identity",
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="audience_role",
            field=models.CharField(
                blank=True, default="", max_length=20,
                choices=[("STUDENT", "Student"), ("TEACHER", "Teacher"),
                         ("ADMIN", "Admin"), ("COUNSELOR", "Counselor")],
                help_text=("Optional. Restrict to one dashboard ROLE; blank = all. "
                           "Coarse — prefer audience_identity for profile isolation."),
            ),
        ),
    ]
