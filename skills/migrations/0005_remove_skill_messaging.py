# PLACEMENT: skills/migrations/0005_remove_skill_messaging.py  (NEW FILE)
# Drops the dead skills Conversation + Message tables (the old REST messaging,
# now fully replaced by the realtime `chat/` app). Message has a FK to
# Conversation, so it is deleted first.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0004_skillsession_slot_key"),
    ]

    operations = [
        migrations.DeleteModel(name="Message"),
        migrations.DeleteModel(name="Conversation"),
    ]
