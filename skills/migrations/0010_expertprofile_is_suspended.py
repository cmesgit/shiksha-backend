from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0009_expertprofile_availability_slots_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="expertprofile",
            name="is_suspended",
            field=models.BooleanField(default=False),
        ),
    ]
