"""
skills/attendance_models.py — per-user attendance for SkillSession (SkillDev
1-on-1 tutor sessions). Previously this feature had zero participant-level
join/leave/duration tracking of any kind. Mirrors
sessions_app.GroupSessionAttendance / GroupSessionAttendanceInterval exactly
— see skills/services/attendance.py for the populate logic, wired into the
same LiveKit webhook livestream/GroupSession already use.

Additive — no change to skills/models.py besides the import at the bottom.
"""
from django.conf import settings
from django.db import models


class SkillSessionAttendance(models.Model):
    session = models.ForeignKey(
        "skills.SkillSession",
        on_delete=models.CASCADE,
        related_name="attendances",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    total_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("session", "user")
        indexes = [
            models.Index(fields=["session", "user"]),
            models.Index(fields=["session", "joined_at"]),
        ]

    def duration(self):
        if self.total_seconds:
            from datetime import timedelta
            return timedelta(seconds=self.total_seconds)
        if self.joined_at and self.left_at:
            return self.left_at - self.joined_at
        return None


class SkillSessionAttendanceInterval(models.Model):
    """Append-only, one row per join→leave cycle."""
    session = models.ForeignKey(
        "skills.SkillSession",
        on_delete=models.CASCADE,
        related_name="attendance_intervals",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    joined_at = models.DateTimeField()
    left_at = models.DateTimeField(null=True, blank=True)
    closed_by_reconcile = models.BooleanField(default=False)

    class Meta:
        ordering = ["joined_at"]
        indexes = [
            models.Index(fields=["session", "user"]),
            models.Index(fields=["session", "joined_at"]),
            models.Index(fields=["session", "left_at"]),
        ]

    def duration_seconds(self):
        if self.joined_at and self.left_at:
            return int((self.left_at - self.joined_at).total_seconds())
        return 0
