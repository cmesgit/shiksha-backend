# PLACEMENT: backend/backend/forum/migrations/0005_delete_notification.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/forum/migrations/0005_delete_notification.py
#
# Drops forum's Notification table. Depends on notifications/0002 (the row
# copy), so `migrate` can never drop the table before the data is safe —
# regardless of the order apps are migrated in.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("forum", "0004_merge_20260630_1651"),
        ("notifications", "0002_copy_forum_notifications"),
    ]

    operations = [
        migrations.DeleteModel(name="Notification"),
    ]
