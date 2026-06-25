from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0006_expert_location_advertising_subscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillsession",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
