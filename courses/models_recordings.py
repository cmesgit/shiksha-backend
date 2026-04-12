import uuid
from django.db import models
from django.conf import settings


class SessionRecording(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="recordings"
    )

    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recordings"
    )

    title = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    session_date = models.DateField(null=True, blank=True)

    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    bunny_video_id = models.CharField(max_length=255)

    STATUS_CHOICES = [
        (0, "Created"),
        (1, "Uploaded"),
        (2, "Processing"),
        (3, "Transcoding"),
        (4, "Finished"),
        (5, "Error"),
    ]

    status = models.IntegerField(choices=STATUS_CHOICES, default=0)

    thumbnail_url = models.URLField(blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_recordings"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ["-session_date"]

    def __str__(self):
        return self.title
