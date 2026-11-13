import logging
from celery import shared_task
from django.db import transaction
from .models import ResearchSubmission
from .research_workflow import LIVE_STATES, maintain_paper

logger = logging.getLogger(__name__)


@shared_task(name="documents.sweep_research_reviews")
def sweep_research_reviews():
    totals = {"papers": 0, "expired": 0, "reminded": 0, "invited": 0, "failed": 0}
    ids = ResearchSubmission.objects.filter(status__in=LIVE_STATES).order_by("pk").values_list("pk", flat=True)
    for pk in ids.iterator(chunk_size=100):
        try:
            with transaction.atomic():
                paper = ResearchSubmission.objects.select_for_update().get(pk=pk)
                outcome = maintain_paper(paper)
                totals["papers"] += 1
                for key, value in outcome.items():
                    totals[key] += value
        except Exception:
            totals["failed"] += 1
            logger.exception("Research deadline sweep failed for paper %s", pk)
    return totals


# ─────────────────────────────────────────────────────────────────────────────
# Community verification (shiksha-backend/docs/EXPLORE_VERIFICATION.md)
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ Why these exist: `documents/verification.py` grew a complete invite engine
# — invite_reviewers, expire_stale_invites, the reminder and inactivity
# settings — that NOTHING CALLED. Every one of those functions was unit-tested
# and inert, so six of the nineteen admin settings were configurable,
# described in the admin's own summary box, and had no effect at all. A test
# that calls a function directly does not prove anybody else does.
#
# All three are no-ops while GlobalSettings.explore_verification_enabled is
# off, so the beat entries are safe to register before the feature launches.

def _verification_enabled():
    from global_settings.models import GlobalSettings
    return GlobalSettings.load().explore_verification_enabled


@shared_task(name="documents.expire_verification_invites")
def expire_verification_invites():
    """Pass unanswered invites to the next reviewer.

    This is what makes "Time to respond" and "Missed invites in a row" real.
    Hourly is enough — the shortest deadline the admin screen allows is one
    day, so minute-level precision buys nothing and costs a query a minute.
    """
    if not _verification_enabled():
        return {"skipped": "feature off"}
    from . import verification as v
    expired, paused = v.expire_stale_invites()
    if expired:
        logger.info("verification: expired %s invite(s), paused %s reviewer(s)",
                    len(expired), len(paused))
    return {"expired": len(expired), "paused": len(paused)}


@shared_task(name="documents.send_verification_reminders")
def send_verification_reminders():
    """Nudge a reviewer before their invite lapses.

    Separate from the expiry sweep because they answer different questions —
    "is it too late" versus "is it nearly too late" — and because an admin can
    turn reminders off while leaving expiry on.
    """
    from django.utils import timezone

    if not _verification_enabled():
        return {"skipped": "feature off"}

    from .models import InviteState, VerificationInvite, VerificationSettings
    from .verification_views import notify_invite

    settings = VerificationSettings.load()
    if not settings.remind_invited_reviewers:
        return {"skipped": "reminders off"}

    now = timezone.now()
    cutoff = now + timezone.timedelta(days=settings.reminder_days_before)
    due = VerificationInvite.objects.filter(
        state=InviteState.OPEN,
        reminder_sent_at__isnull=True,
        expires_at__isnull=False,
        expires_at__lte=cutoff,
        expires_at__gt=now,
    ).select_related("document", "reviewer")[:200]

    sent = 0
    for invite in due:
        notify_invite(invite, reminder=True)
        # Stamped AFTER the send, so a failure is retried next run rather than
        # silently recorded as delivered.
        VerificationInvite.objects.filter(pk=invite.pk).update(
            reminder_sent_at=timezone.now())
        sent += 1
    if sent:
        logger.info("verification: sent %s reminder(s)", sent)
    return {"reminded": sent}


@shared_task(name="documents.sync_teacher_reviewers")
def sync_teacher_reviewers():
    """Backstop for "Teachers review in the subjects they teach".

    The signal in `verification_signals.py` keeps this current as staffing
    changes; this catches rows that changed while the feature was off, and
    anything a bulk `.update()` wrote without firing a signal — which is how
    most staffing repairs in this codebase are done, deliberately.
    """
    if not _verification_enabled():
        return {"skipped": "feature off"}

    from django.contrib.auth import get_user_model

    from . import verification as v
    from .models import VerificationSettings

    settings = VerificationSettings.load()
    if not settings.teachers_review_their_subjects:
        return {"skipped": "teacher auto-confirm off"}

    try:
        from courses.models import TeachingAssignment
    except Exception:
        return {"skipped": "courses unavailable"}

    # ⚠ teacher__isnull=False is not optional. Prod carries ACTIVE PRIMARY
    # rows with nobody in them (TeachingAssignment.teacher is SET_NULL), and
    # without this the values_list yields None.
    teacher_ids = set(
        TeachingAssignment.objects.filter(
            is_active=True, teacher__isnull=False,
        ).values_list("teacher_id", flat=True)
    )
    confirmed = 0
    for user in get_user_model().objects.filter(pk__in=teacher_ids):
        confirmed += len(v.sync_teacher_specializations(user, settings))
    if confirmed:
        logger.info("verification: confirmed %s teacher subject(s)", confirmed)
    return {"confirmed": confirmed}
