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

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from sessions_app.models import PrivateSession, StudyGroupSession
from sessions_app.views import _end_session_internal


# Must match AUTO_EXPIRE_DELAY in consumers.py
GRACE_PERIOD = timedelta(minutes=5)
# Must match STUDY_GROUP_AUTO_EXPIRE_DELAY in consumers.py
STUDY_GROUP_GRACE_PERIOD = timedelta(minutes=7)
# How long a scheduled-but-never-opened study group lingers on the
# Invitations tab before being marked "Not attended" and moved to
# History. Measured from scheduled_date + scheduled_time.
STUDY_GROUP_UNATTENDED_GRACE = timedelta(hours=6)


class Command(BaseCommand):
    help = (
        "Auto-end private sessions where all participants left 5+ minutes "
        "ago, and study groups where all participants left 7+ minutes ago. "
        "Also hard-expires study groups whose selected duration has elapsed, "
        "and flags scheduled study groups that nobody attended within 6h "
        "of their start time."
    )

    def handle(self, *args, **options):
        # ── Private sessions (unchanged behaviour) ───────────────────
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

        # ── Study groups: idle cleanup ───────────────────────────────
        from sessions_app.study_group_views import _end_study_group_internal

        sg_cutoff = timezone.now() - STUDY_GROUP_GRACE_PERIOD
        sg_orphaned = StudyGroupSession.objects.filter(
            status="live",
            all_left_at__isnull=False,
            all_left_at__lte=sg_cutoff,
            active_connections__lte=0,
        )
        sg_count = 0
        for session in sg_orphaned:
            if _end_study_group_internal(session, reason="cleanup_command_idle"):
                sg_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  Auto-ended study group {session.id}")
                )

        # ── Study groups: hard-duration safety net ───────────────────
        now = timezone.now()
        live = StudyGroupSession.objects.filter(
            status="live", room_started_at__isnull=False,
        )
        sg_hard = 0
        for session in live:
            end_at = session.room_started_at + timedelta(minutes=session.duration_minutes)
            if now >= end_at:
                if _end_study_group_internal(session, reason="cleanup_command_duration"):
                    sg_hard += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Duration-expired study group {session.id}"
                        )
                    )

        # ── Study groups: unattended grace window ────────────────────
        # A study group that was scheduled but never opened (nobody
        # joined) lingers in the Invitations / Upcoming tabs for up to
        # STUDY_GROUP_UNATTENDED_GRACE after its scheduled start. After
        # that we flag it "expired" and leave `cancel_reason` empty so
        # the frontend can show "Not attended" (the distinguishing
        # marker is status == 'expired' AND room_started_at is NULL).
        scheduled_groups = StudyGroupSession.objects.filter(
            status="scheduled", room_started_at__isnull=True,
        )
        sg_unattended = 0
        for session in scheduled_groups:
            try:
                scheduled_dt = timezone.make_aware(
                    datetime.combine(session.scheduled_date, session.scheduled_time)
                )
            except Exception:
                # Data glitch: if we can't compute, skip and log.
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped {session.id}: bad scheduled_date/time."
                    )
                )
                continue

            if now >= scheduled_dt + STUDY_GROUP_UNATTENDED_GRACE:
                session.status = "expired"
                session.ended_at = now
                session.save(update_fields=["status", "ended_at", "updated_at"])
                sg_unattended += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Marked study group {session.id} as Not attended"
                    )
                )

        total_sg = sg_count + sg_hard + sg_unattended
        if total_sg:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Cleaned up {total_sg} study group(s) "
                    f"({sg_count} idle, {sg_hard} duration, "
                    f"{sg_unattended} not attended)."
                )
            )
        else:
            self.stdout.write("No orphaned study groups found.")
