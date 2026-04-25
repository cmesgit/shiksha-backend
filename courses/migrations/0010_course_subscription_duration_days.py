from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0009_rename_updated_at_videoprogress_last_watched_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='subscription_duration_days',
            field=models.PositiveIntegerField(
                default=30,
                help_text='How many days of access a single approved enrollment grants (default = 1 month)',
            ),
        ),
    ]
