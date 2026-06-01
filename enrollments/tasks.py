from datetime import timedelta

from config.celery import app


@app.task
def expire_subscriptions():
    """Daily housekeeping for the Subscription table.

    Four things happen each run:
      1. Trials past their end → flip to EXPIRED and send "trial ended" email.
      2. Paid subs past their end → flip to EXPIRED (no email; that's a future task).
      3. Trials with ~7 days remaining → send "7 days left" nudge.
      4. Trials with ~2 days remaining → send "2 days left, upgrade now" nudge.

    All email sends are idempotent via dedicated ``*_sent_at`` flags on the
    Subscription row — re-running the task or rolling beat forward does not
    re-spam users.
    """
    from django.utils import timezone

    from enrollments.models import Subscription
    from enrollments.services import (
        send_trial_ended_email,
        send_trial_reminder_email,
    )

    now = timezone.now()
    summary = {"trials_expired": 0, "paid_expired": 0, "reminders_7d": 0, "reminders_2d": 0}

    # 1) Expire trials and email each one
    expiring_trials = list(
        Subscription.objects
        .filter(
            status=Subscription.STATUS_ACTIVE,
            kind=Subscription.KIND_TRIAL,
            expires_at__lte=now,
        )
        .select_related("user", "course")
    )
    for sub in expiring_trials:
        sub.status = Subscription.STATUS_EXPIRED
        update_fields = ["status", "updated_at"]
        if sub.trial_ended_email_sent_at is None:
            sub.trial_ended_email_sent_at = now
            update_fields.append("trial_ended_email_sent_at")
        sub.save(update_fields=update_fields)
        if "trial_ended_email_sent_at" in update_fields:
            send_trial_ended_email(sub)
    summary["trials_expired"] = len(expiring_trials)

    # 2) Expire paid subs in bulk (no email)
    summary["paid_expired"] = (
        Subscription.objects
        .filter(
            status=Subscription.STATUS_ACTIVE,
            kind=Subscription.KIND_PAID,
            expires_at__lte=now,
        )
        .update(status=Subscription.STATUS_EXPIRED)
    )

    # 3) 7-day reminder — wider window catches missed runs; flag prevents repeats
    seven_day_trials = list(
        Subscription.objects
        .filter(
            kind=Subscription.KIND_TRIAL,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=now + timedelta(days=6),
            expires_at__lte=now + timedelta(days=8),
            trial_reminder_7d_sent_at__isnull=True,
        )
        .select_related("user", "course")
    )
    for sub in seven_day_trials:
        send_trial_reminder_email(sub, days_left=7)
        sub.trial_reminder_7d_sent_at = now
        sub.save(update_fields=["trial_reminder_7d_sent_at", "updated_at"])
    summary["reminders_7d"] = len(seven_day_trials)

    # 4) 2-day reminder
    two_day_trials = list(
        Subscription.objects
        .filter(
            kind=Subscription.KIND_TRIAL,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=now,
            expires_at__lte=now + timedelta(days=2),
            trial_reminder_2d_sent_at__isnull=True,
        )
        .select_related("user", "course")
    )
    for sub in two_day_trials:
        send_trial_reminder_email(sub, days_left=2)
        sub.trial_reminder_2d_sent_at = now
        sub.save(update_fields=["trial_reminder_2d_sent_at", "updated_at"])
    summary["reminders_2d"] = len(two_day_trials)

    return summary
