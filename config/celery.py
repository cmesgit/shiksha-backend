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
    "expire-subscriptions": {
        "task": "enrollments.tasks.expire_subscriptions",
        "schedule": crontab(hour=2, minute=15),  # daily at 02:15
    },
    "expire-chat-attachments": {
        "task": "chat.tasks.expire_old_attachments",
        "schedule": crontab(hour=3, minute=30),  # daily, off-peak
    },
})
