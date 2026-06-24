# skills/migrations/0004_skillsession_slot_key.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0003_expertprofile_availability_slots"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillsession",
            name="slot_key",
            field=models.CharField(
                blank=True,
                max_length=16,
                help_text="Reserved availability slot, e.g. '3-1' (day-slot index).",
            ),
        ),
    ]
