from config.celery import app


@app.task
def expire_subscriptions():
    """Daily housekeeping for the Subscription table.

    Flip any ACTIVE subscription whose end date has passed to EXPIRED. The
    platform is on the free model and no longer tracks per-course trials, so
    this is a single bulk update with no per-kind branching or email sends.
    (Trial reminder/expiry emails were removed along with the trial fields.)
    """
    from django.utils import timezone
    from enrollments.models import Subscription

    now = timezone.now()
    expired = (
        Subscription.objects
        .filter(status=Subscription.STATUS_ACTIVE, expires_at__lte=now)
        .update(status=Subscription.STATUS_EXPIRED)
    )
    return {"expired": expired}
