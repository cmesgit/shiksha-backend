import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("shiksha")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ── Celery Beat Schedule ──────────────────────────────
app.conf.beat_schedule = {
    "notify-session-starting-soon": {
        "task": "activity.tasks.notify_session_starting_soon",
        "schedule": crontab(minute="*/15"),  # every 15 minutes
    },
}
