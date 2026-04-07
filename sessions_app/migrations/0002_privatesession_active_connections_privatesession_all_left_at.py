from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sessions_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="privatesession",
            name="active_connections",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="privatesession",
            name="all_left_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
