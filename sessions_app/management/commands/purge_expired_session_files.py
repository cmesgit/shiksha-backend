"""
Delete session files past their retention window.

Sits beside cleanup_expired_sessions.py and follows the same shape.

Usage:
    python manage.py purge_expired_session_files

Run via cron, e.g.:
    */15 * * * * cd /path/to/project && python manage.py purge_expired_session_files
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from sessions_app.models import SessionFile, PrivateSessionFile


class Command(BaseCommand):
    help = "Purge session files whose retention window has passed."

    def handle(self, *args, **options):
        count = 0
        for model in (SessionFile, PrivateSessionFile):
            rows = model.objects.filter(
                expires_at__lte=timezone.now(), saved_to_course=False
            )
            for row in rows.iterator():
                row.file.delete(save=False)
                row.delete()
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Purged {count} expired session files."))
