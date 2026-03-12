import uuid
from django.db import models
from django.conf import settings
from .models import Subject, Chapter


class SessionRecording(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="recordings"
    )

    chapter = models.ForeignKey(
        Chapter,
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