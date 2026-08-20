# PLACEMENT: backend/backend/notifications/apps.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/apps.py

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"

    def ready(self):
        # Registers the deploy-time guard that a time-critical notification
        # never routes only to channels this box hasn't configured.
        from . import checks  # noqa: F401
