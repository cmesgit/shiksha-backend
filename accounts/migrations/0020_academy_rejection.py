from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0019_agreement_letters"),
    ]

    operations = [
        migrations.AddField(
            model_name="teacherprofile",
            name="academy_rejection_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="academy_rejected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="teacherprofile",
            name="academy_status",
            field=models.CharField(
                choices=[
                    ("locked", "Locked"),
                    ("pending", "Pending review"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                ],
                default="locked", max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="teacherprofile",
            name="skill_status",
            field=models.CharField(
                choices=[
                    ("locked", "Locked"),
                    ("pending", "Pending review"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                ],
                default="locked", max_length=10,
            ),
        ),
    ]
