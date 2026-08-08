from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quizzes', '0015_question_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='reveal_answers_after',
            field=models.PositiveIntegerField(default=1),
        ),
    ]
