from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_teacherprofile_alter_role_options_alter_user_options_and_more"),
    ]

    operations = [
        # Profile form fillup fields
        migrations.AddField(
            model_name="profile",
            name="date_of_birth",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="father_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="father_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name="profile",
            name="mother_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="guardian",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="guardian_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name="profile",
            name="current_address",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="profile",
            name="permanent_address",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="profile",
            name="same_as_current",
            field=models.BooleanField(default=False),
        ),
        # TeacherProfile form fillup fields
        migrations.AddField(
            model_name="teacherprofile",
            name="gender",
            field=models.CharField(
                blank=True,
                choices=[("male", "Male"), ("female", "Female"), ("other", "Other")],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="date_of_birth",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="father_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="father_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="mother_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="mother_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="current_address",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="permanent_address",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="same_as_current",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="highest_qualification",
            field=models.CharField(
                blank=True,
                choices=[
                    ("high_school", "High School"),
                    ("intermediate", "Intermediate"),
                    ("bachelors", "Bachelor's Degree"),
                    ("masters", "Master's Degree"),
                    ("phd", "Ph.D."),
                    ("bed", "B.Ed."),
                    ("med", "M.Ed."),
                    ("diploma", "Diploma"),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="other_qualification",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="subject_specialization",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="teaching_experience_years",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="previous_institution",
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
