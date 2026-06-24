# accounts/migrations/0014_remove_teacher_password.py
#
# REFACTOR: removes the separate teacher_password field from TeacherProfile.
#
# Teacher context is now entered with the account password (same one used to
# log in). No separate teacher-door password exists anymore.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_teacherprofile_teacher_password"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="teacherprofile",
            name="teacher_password",
        ),
    ]
