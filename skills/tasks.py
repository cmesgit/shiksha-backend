# skills/tasks.py
#
# Auto-decline pending SkillSession requests past the design's 24h SLA
# (WORKFLOW.md §2/§3: "Requests expire in 24h — auto-decline + refund").
# Mirrors livestream.tasks's sweep-task shape (a periodic Celery task that
# advances a status on a timer rather than only on read).

from django.utils import timezone

from config.celery import app
from .models import SkillSession
from .notifications import push_skill_bell


@app.task
def auto_decline_stale_requests():
    """Flip any REQUESTED session older than 24h to AUTO_DECLINED and
    release its held slot. Runs on a schedule (see config/celery.py); never
    silently loses a request — every one ends up DECLINED, AUTO_DECLINED, or
    CONFIRMED, never stuck in REQUESTED indefinitely."""
    from .teacher_views import free_slot

    cutoff = timezone.now() - timezone.timedelta(hours=24)
    stale = SkillSession.objects.filter(
        status=SkillSession.STATUS_REQUESTED, created_at__lt=cutoff
    ).select_related("expert")

    count = 0
    for sess in stale:
        sess.status = SkillSession.STATUS_AUTO_DECLINED
        sess.save(update_fields=["status", "updated_at"])
        if sess.slot_key:
            free_slot(sess.expert, sess.slot_key)
        push_skill_bell(sess, "declined")
        count += 1
    return count
