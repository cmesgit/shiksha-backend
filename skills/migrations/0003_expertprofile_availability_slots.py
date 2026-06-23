# skills/migrations/0003_expertprofile_availability_slots.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0002_skillcourse_skillcourseenrollment_skillcoursesection_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="expertprofile",
            name="availability_slots",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Weekly availability. Shape: {"open":["0-1","2-3"], "booked":["1-0"]}',
            ),
        ),
    ]
