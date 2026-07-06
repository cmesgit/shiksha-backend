# Re-adds the Enrollment -> Batch foreign key.
#
# History: added in enrollments/0006_enrollment_batch_and_more, then dropped in
# enrollments/0009 during the learner-profile refactor. Dropping it left
# Batch.seats_taken / Batch.is_full and BatchAdmin's Count("enrollments")
# pointing at a reverse relation that no longer existed (a runtime crash on the
# Batch admin page). Restoring the FK with related_name="enrollments" fixes
# those and is the foundation the per-batch progress feature builds on.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("courses", "0014_merge_20260630_1651"),
        ("enrollments", "0009_remove_enrollmentrequest_unique_pending_request_per_user_course_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="enrollment",
            name="batch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="enrollments",
                to="courses.batch",
            ),
        ),
        migrations.AddIndex(
            model_name="enrollment",
            index=models.Index(fields=["batch", "status"], name="enroll_batch_status_idx"),
        ),
    ]
