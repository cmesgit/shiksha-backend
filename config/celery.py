# PLACEMENT: backend/backend/config/celery.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/config/celery.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# Adds ONE beat entry: sync-open-session-statuses (every minute), which drives
# livestream.tasks.sync_open_session_statuses — the sweep that advances the
# live-session reconnection ladder on a timer and keeps the stored `status`
# column in sync with computed_status(). Everything else is unchanged.
#
# Requires a running `celery beat` process (you already run one for the
# existing schedule). If beat is NOT running, sessions still resolve on read
# via computed_status() and on the LiveKit room_finished webhook — this sweep
# is what makes the intermediate ladder states (RECONNECTING/PAUSED) advance
# without someone reading them.

import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("shiksha")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# autodiscover_tasks() only finds tasks.py inside INSTALLED_APPS — "config"
# isn't an app, so its maintenance_tasks module needs an explicit import to
# actually register the tasks referenced in the beat schedule below.
from config import maintenance_tasks  # noqa: F401,E402

# ── Celery Beat Schedule ──────────────────────────────
# NOTE: update(), not assignment — config_from_object() above already
# populated app.conf.beat_schedule from settings.CELERY_BEAT_SCHEDULE
# (which registers "notifications-session-reminders",
# config/settings_base.py). A plain `=` here would silently discard that
# entry, which is exactly what happened before this fix — the "starts in
# 1h/24h" reminder job never ran.
app.conf.beat_schedule.update({
    "notify-session-starting-soon": {
        "task": "activity.tasks.notify_session_starting_soon",
        "schedule": crontab(minute="*/15"),
    },
    "sync-open-session-statuses": {
        "task": "livestream.tasks.sync_open_session_statuses",
        "schedule": crontab(minute="*/1"),  # advance the live-session ladder
    },
    "auto-complete-expired-sessions": {
        "task": "livestream.tasks.auto_complete_expired_sessions",
        "schedule": crontab(minute="*/5"),  # safety net
    },
    "sample-live-viewers": {
        "task": "livestream.tasks.sample_live_viewers",
        "schedule": crontab(minute="*/1"),  # viewer snapshots + attendance reconcile
    },
    "expire-subscriptions": {
        "task": "enrollments.tasks.expire_subscriptions",
        "schedule": crontab(hour=2, minute=15),  # daily at 02:15
    },
    "expire-chat-attachments": {
        "task": "chat.tasks.expire_old_attachments",
        "schedule": crontab(hour=3, minute=30),  # daily, off-peak
    },
    "auto-decline-stale-skill-requests": {
        "task": "skills.tasks.auto_decline_stale_requests",
        "schedule": crontab(minute="*/15"),  # 24h SLA sweep
    },
    "expire-scholarship-exam-sessions": {
        "task": "scholarship.tasks.expire_exam_sessions",
        "schedule": crontab(minute="*/1"),  # backstop only — views.py enforces the deadline on read/write
    },
    "expire-scholarship-awards": {
        "task": "scholarship.tasks.expire_scholarship_awards",
        "schedule": crontab(hour=2, minute=45),  # daily, off-peak
    },
    "relay-chat-outbox": {
        "task": "chat.tasks.relay_outbox_task",
        # Beat has no sub-minute crontab syntax — a plain number is a
        # fixed-interval (seconds) schedule instead. chat/tasks.py's own
        # docstring has said "every ~10s" since this task was written; this
        # entry was simply missing.
        "schedule": 10.0,
    },
    "cleanup-expired-sessions": {
        "task": "config.maintenance_tasks.cleanup_expired_sessions_task",
        "schedule": crontab(minute="*/3"),  # matches the command's own recommended cadence
    },
    "cleanup-unverified-users": {
        "task": "config.maintenance_tasks.cleanup_unverified_users_task",
        "schedule": crontab(hour=4, minute=0),  # daily, off-peak
    },
    "cleanup-orphan-material-files": {
        "task": "config.maintenance_tasks.cleanup_orphan_material_files_task",
        "schedule": crontab(hour=3, minute=0),  # daily, off-peak
    },
})
