import uuid
from django.db import models
from django.conf import settings


class LiveSession(models.Model):
    STATUS_SCHEDULED = "SCHEDULED"
    STATUS_LIVE = "LIVE"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_LIVE, "Live"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    room_name = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_live_sessions",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["course"]),
            models.Index(fields=["subject"]),
            models.Index(fields=["start_time"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"


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
        ]
