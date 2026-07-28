from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0024_backfill_course_board_slugs'),
    ]

    operations = [
        migrations.AlterField(
            model_name='board',
            name='slug',
            field=models.SlugField(blank=True, max_length=140, unique=True),
        ),
        migrations.AlterField(
            model_name='course',
            name='slug',
            field=models.SlugField(blank=True, max_length=220, unique=True),
        ),
    ]
