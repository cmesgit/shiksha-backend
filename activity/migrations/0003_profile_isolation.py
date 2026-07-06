# activity/migrations/0003_profile_isolation.py   (NEW FILE)
#
# Adds Activity.audience + Activity.learner_profile and backfills
# audience for existing rows from `type`:
#
#     SUBMISSION rows have only ever been written for teachers
#     (see activity/signals.py — _notify_teacher), everything else
#     (ASSIGNMENT / QUIZ / SESSION) only for enrolled learners
#     (_bulk_notify_students). The backfill therefore reproduces the
#     writers' intent exactly.
#
# learner_profile stays NULL for legacy learner rows — the feed treats
# NULL as "visible to every learner profile of the account", so nothing
# disappears from anyone's bell; old rows simply age out.
#
# Both operations are additive and reversible. Safe to run on a live DB
# (the data step is a single UPDATE per audience value).

from django.db import migrations, models
import django.db.models.deletion


def backfill_audience(apps, schema_editor):
    Activity = apps.get_model("activity", "Activity")
    Activity.objects.filter(type="SUBMISSION").update(audience="TEACHER")
    Activity.objects.exclude(type="SUBMISSION").update(audience="LEARNER")


def noop(apps, schema_editor):
    # Reverse: the column default ("LEARNER") is fine; nothing to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),  # LearnerProfile — bump if your accounts head differs
        ("activity", "0002_activity_subject_id_activity_subject_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="audience",
            field=models.CharField(
                choices=[("LEARNER", "Learner"), ("TEACHER", "Teacher")],
                db_index=True,
                default="LEARNER",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="activity",
            name="learner_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="activities",
                to="accounts.learnerprofile",
            ),
        ),
        migrations.AddIndex(
            model_name="activity",
            index=models.Index(
                fields=["user", "audience", "learner_profile", "-created_at"],
                name="activity_feed_hotpath_idx",
            ),
        ),
        migrations.RunPython(backfill_audience, noop),
    ]
