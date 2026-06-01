from django.db import migrations, models


def backfill_kind_to_paid(apps, schema_editor):
    """Every Subscription that existed before trials = an approved paid enrollment."""
    Subscription = apps.get_model("enrollments", "Subscription")
    Subscription.objects.all().update(kind="PAID")


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("enrollments", "0006_enrollment_batch_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="kind",
            field=models.CharField(
                choices=[("TRIAL", "Trial"), ("PAID", "Paid")],
                default="PAID",
                help_text="TRIAL = free 30-day trial; PAID = approved enrollment.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="subscription",
            name="trial_reminder_7d_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="trial_reminder_2d_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="trial_ended_email_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_kind_to_paid, reverse_noop),
        migrations.AddIndex(
            model_name="subscription",
            index=models.Index(
                fields=["kind", "status", "expires_at"],
                name="enrollments_kind_status_exp_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.UniqueConstraint(
                fields=["user", "course"],
                condition=models.Q(kind="TRIAL"),
                name="unique_trial_per_user_course",
            ),
        ),
    ]
