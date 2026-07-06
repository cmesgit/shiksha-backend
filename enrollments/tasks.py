# PLACEMENT: backend/backend/enrollments/tasks.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/enrollments/tasks.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The old task flipped expired Subscriptions → EXPIRED but left the matching
# Enrollment rows ACTIVE forever. That split the access model in two:
#   • the CONTENT gate (has_active_subscription) checks the subscription → OK,
#   • but the class-chat gate (chat.services.learner_in_course) checks the
#     Enrollment row → a student with a lapsed subscription kept chat access
#     and still showed as ACTIVE on the admin batch roster.
#
# Now, for every subscription that expires in this run, the matching
# (learner_profile, course) Enrollment is flipped to REVOKED in the same pass,
# so "enrolled" means the same thing to every feature.
#
# NOTE: this pairs with the chat/services.py patch (learner_in_course now
# prefers has_active_subscription). Either fix alone closes the gap; applying
# both makes the enrollment row and the subscription agree AND makes the chat
# gate authoritative on the subscription. No migration required — REVOKED is an
# existing Enrollment status.

from config.celery import app


@app.task
def expire_subscriptions():
    """Daily housekeeping: expire lapsed subscriptions AND revoke the
    enrollment rows they granted, so access is consistent across features.

    Returns a small dict for the beat log: how many subscriptions expired and
    how many enrollments were revoked as a result.
    """
    from django.utils import timezone
    from django.db import transaction
    from enrollments.models import Subscription, Enrollment

    now = timezone.now()

    with transaction.atomic():
        # Capture which (learner, course) pairs are lapsing BEFORE the update,
        # so we know exactly which enrollments to revoke.
        lapsing = list(
            Subscription.objects
            .filter(status=Subscription.STATUS_ACTIVE, expires_at__lte=now)
            .values_list("learner_profile_id", "course_id")
        )

        expired = (
            Subscription.objects
            .filter(status=Subscription.STATUS_ACTIVE, expires_at__lte=now)
            .update(status=Subscription.STATUS_EXPIRED)
        )

        revoked = 0
        for learner_id, course_id in lapsing:
            if learner_id is None:
                # Legacy account-level subscription with no profile link — there
                # is no profile-scoped enrollment to revoke; skip safely.
                continue
            # Only revoke if this learner has NO other still-active subscription
            # for the course (covers overlapping/extended grants).
            still_active = (
                Subscription.objects
                .filter(
                    learner_profile_id=learner_id,
                    course_id=course_id,
                    status=Subscription.STATUS_ACTIVE,
                    expires_at__gt=now,
                )
                .exists()
            )
            if still_active:
                continue
            revoked += (
                Enrollment.objects
                .filter(
                    learner_profile_id=learner_id,
                    course_id=course_id,
                    status=Enrollment.STATUS_ACTIVE,
                )
                .update(status=Enrollment.STATUS_REVOKED)
            )

    return {"expired": expired, "enrollments_revoked": revoked}
