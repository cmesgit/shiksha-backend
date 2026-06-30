from django.db import migrations, models


class Migration(migrations.Migration):
    """Add a place to store the signed faculty agreement.

    This is the ONLY schema change for the faculty-signup feature. The expanded
    class/stream taxonomy needs no migration because those values live in
    choice-less JSONFields (TeacherProfile.classes/.streams and
    TeacherCourseApplication.classes/.streams).

    The signed PDF is collected from the dashboard /form-fillup form after the
    user verifies their email (Approach A — the JSON signup endpoint can't carry
    files, and a just-signed-up user isn't verified yet).
    """

    dependencies = [
        ("accounts", "0015_teacher_track_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="teacherprofile",
            name="signed_agreement",
            field=models.FileField(
                upload_to="teachers/agreements/", null=True, blank=True
            ),
        ),
    ]
