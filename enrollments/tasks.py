from config.celery import app


@app.task
def expire_subscriptions():
    """Flip ACTIVE Subscriptions whose expires_at has passed to EXPIRED.

    Runs nightly via Celery beat. The user-facing API computes is_active in
    real time, so this is purely for keeping the DB status field consistent
    with reality (admin reports, queries, etc.).
    """
    from django.utils import timezone

    from enrollments.models import Subscription

    now = timezone.now()
    qs = Subscription.objects.filter(
        status=Subscription.STATUS_ACTIVE,
        expires_at__lte=now,
    )
    count = qs.update(status=Subscription.STATUS_EXPIRED)
    return f"Expired {count} subscriptions"
