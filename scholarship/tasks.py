"""Backstop sweeps. Correctness of the exam deadline does NOT depend on
these — every read/write path in views.py calls services.expire_if_past_deadline
first, so a session is scored the moment anyone (student or admin) touches it
past its deadline. These sweeps only catch sessions nobody ever reloads
(tab closed and never reopened) and awards that outlive their academic year."""
from config.celery import app


@app.task
def expire_exam_sessions():
    """Auto-submit any in-progress session whose deadline has passed."""
    from django.utils import timezone

    from . import services
    from .models import ExamSession

    stale_ids = list(
        ExamSession.objects.filter(
            status=ExamSession.STATUS_IN_PROGRESS, deadline__lte=timezone.now(),
        ).values_list("id", flat=True)
    )
    expired = 0
    for session_id in stale_ids:
        session = ExamSession.objects.get(pk=session_id)
        services.submit_exam(session, auto_expired=True)
        expired += 1
    return {"expired": expired}


@app.task
def expire_scholarship_awards():
    """Daily housekeeping: flip awards past their academic-year expiry."""
    from django.utils import timezone

    from .models import ScholarshipAward

    expired = (
        ScholarshipAward.objects
        .filter(status__in=[ScholarshipAward.STATUS_LOCKED, ScholarshipAward.STATUS_ACTIVE], expires_at__lte=timezone.now())
        .update(status=ScholarshipAward.STATUS_EXPIRED)
    )
    return {"expired": expired}
