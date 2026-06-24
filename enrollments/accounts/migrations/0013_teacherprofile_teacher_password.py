# accounts/migrations/0013_teacherprofile_teacher_password.py
#
# Adds a SEPARATE teacher-auth password to TeacherProfile.
#
# Rationale (from the multi-identity refactor):
#   One email == one User == one ACCOUNT password (used for learner login).
#   The teacher identity now has its OWN password, stored hashed here, so
#   entering teacher context is an independent "door". Blank = not yet set
#   (e.g. legacy teacher rows created before this migration); those fall
#   back to the account password the first time they enter teacher mode and
#   are prompted to set a teacher password.
#
# NOTE: rename the dependency below if your latest accounts migration is not
# 0012_passwordresetcode.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_passwordresetcode"),
    ]

    operations = [
        migrations.AddField(
            model_name="teacherprofile",
            name="teacher_password",
            field=models.CharField(
                blank=True,
                default="",
                max_length=128,
                help_text="Hashed teacher-context password. Blank = not set yet.",
            ),
        ),
    ]
