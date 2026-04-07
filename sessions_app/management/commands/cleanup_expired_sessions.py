"""
Safety-net management command to auto-end private sessions that have been
empty (all participants left) for longer than the grace period.

This catches edge cases where Daphne restarted and the in-memory asyncio
timer was lost.

Usage:
    python manage.py cleanup_expired_sessions

Run via cron every few minutes on the server, e.g.:
    */3 * * * * cd /path/to/project && python manage.py cleanup_expired_sessions
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from sessions_app.models import PrivateSession
from sessions_app.views import _end_session_internal


# Must match AUTO_EXPIRE_DELAY in consumers.py
GRACE_PERIOD = timedelta(minutes=5)


class Command(BaseCommand):
    help = "Auto-end private sessions where all participants left 5+ minutes ago."

    def handle(self, *args, **options):
        cutoff = timezone.now() - GRACE_PERIOD

        orphaned = PrivateSession.objects.filter(
            status="ongoing",
            all_left_at__isnull=False,
            all_left_at__lte=cutoff,
            active_connections__lte=0,
        )

        count = 0
        for session in orphaned:
            ended = _end_session_internal(session, reason="cleanup_command")
            if ended:
                count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  Auto-ended session {session.id}")
                )

        if count:
            self.stdout.write(
                self.style.SUCCESS(f"Cleaned up {count} expired session(s).")
            )
        else:
            self.stdout.write("No orphaned sessions found.")
