# PLACEMENT: backend/backend/notifications/tasks.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/tasks.py
#
# Three tasks:
#   deliver_email_task / deliver_sms_task
#       The async legs of notify(). Retried with backoff — a Resend or
#       MSG91 blip must not lose a booking confirmation. services.py
#       calls .delay() and falls back to a synchronous call if the broker
#       is down (same pattern livestream's WS push already uses).
#   send_session_reminders
#       Celery-beat sweep (every NOTIFY_REMINDER_SWEEP_MINUTES). Finds
#       counseling appointments, private sessions and group sessions
#       whose start time falls inside [now+offset, now+offset+sweep) for
#       each offset in NOTIFY_REMINDER_OFFSETS_MIN, and notifies every
#       party once — ReminderLog's unique constraint makes re-runs and
#       overlapping sweeps idempotent.

import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Delivery legs ───────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=30,
             retry_backoff=True, name="notifications.deliver_email")
def deliver_email_task(self, email_addr, subject, body):
    from accounts.email_utils import send_gmail
    try:
        send_gmail(email_addr, subject, body)
    except Exception as exc:
        logger.warning("email retry %s → %s (%s)", self.request.retries,
                       email_addr, exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30,
             retry_backoff=True, name="notifications.deliver_sms")
def deliver_sms_task(self, user_id, to, template_key, variables, verb,
                     phone_source=""):
    from django.contrib.auth import get_user_model
    from .sms import send_sms

    user = None
    if user_id:
        user = get_user_model().objects.filter(pk=user_id).first()
    ok = send_sms(to=to, template_key=template_key, variables=variables,
                  verb=verb, user=user, phone_source=phone_source)
    # send_sms never raises; retry only on provider failure (a skipped
    # SMS — no phone, no template — will skip identically forever).
    if not ok:
        from .models import SmsLog
        last = SmsLog.objects.filter(to=to, verb=verb).order_by("-id").first()
        if last is not None and last.status == SmsLog.STATUS_FAILED:
            raise self.retry(exc=Exception(last.error or "provider failure"))


# ── Reminder sweep ──────────────────────────────────────────────────────

def _fmt_when(dt):
    """'06:30 PM, 14 Jul' in local (Asia/Kolkata) time — used both in the
    notification body and as the {when} SMS template variable."""
    return timezone.localtime(dt).strftime("%I:%M %p, %d %b")


def _aware(d, t):
    """Combine a naive DateField+TimeField pair (PrivateSession /
    GroupSession store these) into an aware datetime in local time."""
    return timezone.make_aware(datetime.combine(d, t),
                               timezone.get_default_timezone())


def _remind(kind, object_id, offset, recipients):
    """Send one reminder to each (user, verb, title, body, sms_vars,
    learner_profile, link) tuple — guarded by the ReminderLog dedupe row."""
    from .models import ReminderLog
    from .services import notify

    _, created = ReminderLog.objects.get_or_create(
        kind=kind, object_id=str(object_id), offset_minutes=offset)
    if not created:
        return 0
    sent = 0
    for (user, verb, title, body, sms_vars, learner_profile, link) in recipients:
        n = notify(recipient=user, verb=verb, title=title, body=body,
                   link_url=link, payload={"kind": kind, "id": str(object_id)},
                   sms_vars=sms_vars, learner_profile=learner_profile)
        sent += 1 if n is not None else 0
    return sent


@shared_task(name="notifications.send_session_reminders")
def send_session_reminders():
    """Beat entry point. Window math: for each offset O and sweep period
    S, remind everything starting in [now+O, now+O+S). Consecutive runs
    tile the timeline with no gaps; ReminderLog absorbs any overlap."""
    from counseling.models import Appointment
    from sessions_app.models import GroupSession, GroupSessionInvite, PrivateSession

    now = timezone.now()
    sweep = timedelta(minutes=getattr(settings, "NOTIFY_REMINDER_SWEEP_MINUTES", 5))
    offsets = getattr(settings, "NOTIFY_REMINDER_OFFSETS_MIN", [1440, 60])
    total = 0

    for offset in offsets:
        lo = now + timedelta(minutes=offset)
        hi = lo + sweep
        tag = "24h" if offset >= 180 else "1h"

        # ── Counseling appointments ────────────────────────────────────
        appts = (Appointment.objects
                 .filter(status=Appointment.STATUS_CONFIRMED,
                         scheduled_at__gte=lo, scheduled_at__lt=hi)
                 .select_related("learner_profile", "booked_by",
                                 "counselor__user"))
        for appt in appts:
            when = _fmt_when(appt.scheduled_at)
            verb = f"counseling.reminder_{tag}"
            student_title = f"Upcoming session with {appt.counselor.display_name}"
            counselor_title = f"Upcoming session with {appt.learner_profile.display_name}"
            body = f"Starts at {when}."
            total += _remind(
                "counseling", appt.pk, offset,
                [
                    (appt.booked_by, verb, student_title, body,
                     {"title": f"Counseling with {appt.counselor.display_name}",
                      "when": when},
                     appt.learner_profile,
                     f"/counseling/appointments/{appt.pk}"),
                    (appt.counselor.user, verb, counselor_title, body,
                     {"title": f"Session with {appt.learner_profile.display_name}",
                      "when": when},
                     None,
                     f"/counselor/appointments/{appt.pk}"),
                ])

        # ── Private 1-on-1 sessions ────────────────────────────────────
        # Effective start honours a teacher-proposed reschedule when both
        # fields are set; only "approved" sessions are live bookings. The
        # date filter must OR the rescheduled date, or a session moved to
        # tomorrow would be invisible to tomorrow's sweep.
        from django.db.models import Q
        for ps in (PrivateSession.objects
                   .filter(status="approved")
                   .filter(Q(scheduled_date__range=(lo.date(), hi.date()))
                           | Q(rescheduled_date__range=(lo.date(), hi.date())))
                   .select_related("teacher", "requested_by")):
            eff_date = ps.rescheduled_date or ps.scheduled_date
            eff_time = ps.rescheduled_time or ps.scheduled_time
            start = _aware(eff_date, eff_time)
            if not (lo <= start < hi):
                continue
            when = _fmt_when(start)
            verb = f"session.reminder_{tag}"
            body = f"{ps.subject} starts at {when}."
            sms_vars = {"title": ps.subject[:30], "when": when}
            total += _remind(
                "private_session", ps.pk, offset,
                [
                    (ps.requested_by, verb, f"Upcoming class: {ps.subject}",
                     body, sms_vars, None, "/student/private"),
                    (ps.teacher, verb, f"Upcoming class: {ps.subject}",
                     body, sms_vars, None, "/teacher/private"),
                ])

        # ── Group sessions (host + accepted invitees) ──────────────────
        for gs in (GroupSession.objects
                   .filter(status="scheduled", session_type="scheduled")
                   .filter(scheduled_date__range=(lo.date(), hi.date()))
                   .select_related("host")):
            start = _aware(gs.scheduled_date, gs.scheduled_time)
            if not (lo <= start < hi):
                continue
            when = _fmt_when(start)
            label = gs.topic or gs.subject_name or "Group session"
            body = f"{label} starts at {when}."
            sms_vars = {"title": label[:30], "when": when}
            people = [gs.host] + [
                inv.user for inv in
                GroupSessionInvite.objects.filter(session=gs, status="accepted")
                .select_related("user")
            ]
            total += _remind(
                "group_session", gs.pk, offset,
                [(u, "group.reminder_1h" if tag == "1h" else "group.reminder_24h",
                  f"Upcoming: {label}", body, sms_vars, None,
                  f"/sessions/group/{gs.short_code or gs.pk}")
                 for u in people])

    if total:
        logger.info("reminders: sent %s notification(s)", total)
    return total
