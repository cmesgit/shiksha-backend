import uuid
from django.db import models
from django.conf import settings


class VideoProgress(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="video_progress"
    )

    recording = models.ForeignKey(
        "courses.SessionRecording",
        on_delete=models.CASCADE,
        related_name="progress"
    )

    last_position = models.FloatField(default=0)       # seconds
    completed = models.BooleanField(default=False)
    last_watched_at = models.DateTimeField(auto_now=True)  # ← added

    class Meta:
        unique_together = ("student", "recording")

    def __str__(self):
        return f"{self.student} – {self.recording} @ {self.last_position}s"
