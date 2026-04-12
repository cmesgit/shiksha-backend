import uuid
from django.db import models
from django.conf import settings


class LiveSession(models.Model):
    # 🔥 FULL STATE SYSTEM
    STATUS_SCHEDULED = "SCHEDULED"
    STATUS_WAITING = "WAITING_FOR_TEACHER"
    STATUS_LIVE = "LIVE"
    STATUS_PAUSED = "PAUSED"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_RECONNECTING = "RECONNECTING"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_WAITING, "Waiting for Teacher"),
        (STATUS_LIVE, "Live"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_RECONNECTING, "Reconnecting"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 🔥 CORE FIELD (DO NOT REMOVE)
    # Meaning: "last time teacher disconnected (uncertain state)"
    teacher_left_at = models.DateTimeField(null=True, blank=True)

    # 🔥 OPTIONAL BUT IMPORTANT (future-proofing)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.CASCADE,
        related_name="live_sessions",
    )

    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="live_sessions",
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # 🧠 PLANNING LAYER ONLY
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    room_name = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_live_sessions",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_time"]
        indexes = [
            models.Index(fields=["course"]),
            models.Index(fields=["subject"]),
            models.Index(fields=["start_time"]),
            models.Index(fields=["status"]),
            models.Index(fields=["teacher_left_at"]),
            models.Index(fields=["course", "start_time"]),
            models.Index(fields=["subject", "start_time"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"

    # ✅ UI / analytics only
    def duration(self):
        return self.end_time - self.start_time

    # 🔥 CORE STATE LOGIC (VERY IMPORTANT)
    def computed_status(self):
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()

        if self.status == self.STATUS_CANCELLED:
            return self.STATUS_CANCELLED

        # Already explicitly completed
        if self.status == self.STATUS_COMPLETED:
            return self.STATUS_COMPLETED

        # Session end time has passed → completed
        if now >= self.end_time:
            return self.STATUS_COMPLETED

        # Manual pause takes priority over teacher_left_at timer
        if self.status == self.STATUS_PAUSED and not self.teacher_left_at:
            return self.STATUS_PAUSED

        if self.teacher_left_at:
            diff = now - self.teacher_left_at

            # 0–10 min → reconnecting
            if diff <= timedelta(minutes=10):
                return self.STATUS_RECONNECTING

            # 10–60 min → paused
            if diff <= timedelta(minutes=60):
                return self.STATUS_PAUSED

            # >60 min → completed
            return self.STATUS_COMPLETED

        if self.status == self.STATUS_LIVE and not self.teacher_left_at:
            return self.STATUS_LIVE

        if now < self.start_time:
            return self.STATUS_SCHEDULED

        return self.STATUS_WAITING


class LiveSessionAttendance(models.Model):
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="attendances",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "user")
        indexes = [
            models.Index(fields=["session", "user"]),
            models.Index(fields=["session", "joined_at"]),
        ]

    # ✅ duration tracking (important for analytics)
    def duration(self):
        if self.joined_at and self.left_at:
            return self.left_at - self.joined_at
        return None
