import uuid
from django.db import models
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType


class Activity(models.Model):
    TYPE_ASSIGNMENT = "ASSIGNMENT"
    TYPE_QUIZ = "QUIZ"
    TYPE_SESSION = "SESSION"
    TYPE_SUBMISSION = "SUBMISSION"

    TYPE_CHOICES = [
        (TYPE_ASSIGNMENT, "Assignment"),
        (TYPE_QUIZ, "Quiz"),
        (TYPE_SESSION, "Live Session"),
        (TYPE_SUBMISSION, "Submission"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="activities"
    )

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)

    # Direct subject_id — avoids content_object traversal in serializers
    subject_id = models.UUIDField(null=True, blank=True, db_index=True)
    subject_name = models.CharField(max_length=255, blank=True, default="")

    # Generic relation to the linked object
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")

    due_date = models.DateTimeField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["type"]),
            models.Index(fields=["due_date"]),
            models.Index(fields=["subject_id"]),
        ]

    def __str__(self):
        return f"{self.type} - {self.title}"
