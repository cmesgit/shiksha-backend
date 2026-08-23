"""
activity/models.py  ·  FULL REPLACEMENT — profile-isolated feed rows
────────────────────────────────────────────────────────────────────
Adds two columns that make notification isolation possible:

  audience         which identity of the account the row is FOR.
                   The old table couldn't tell a teacher's
                   "student submitted X" apart from a learner's
                   "new assignment X" on a one-email BOTH account —
                   both landed in the same bell.

  learner_profile  which learner profile the row is for (LEARNER
                   audience only). Two children on one parent email
                   stop seeing each other's assignments.

Both are nullable/defaulted so the migration is additive and the
0003 data migration backfills audience from `type` (SUBMISSION rows
were always teacher-directed; everything else was learner-directed —
that is exactly how activity/signals.py has always written them).
"""

import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Activity(models.Model):
    TYPE_ASSIGNMENT = "ASSIGNMENT"
    TYPE_QUIZ = "QUIZ"
    TYPE_SESSION = "SESSION"
    TYPE_SUBMISSION = "SUBMISSION"
    # Study material upload. Added late: material uploads were the one
    # lifecycle that never wrote an Activity row, so they existed only as a
    # fire-and-forget WS frame and vanished on refresh. Routing them through
    # _bulk_notify_students needs a type of their own — reusing ASSIGNMENT
    # would have shown a handout under an assignment icon and sent the click
    # to the assignments screen.
    TYPE_MATERIAL = "MATERIAL"
    # Class recording published. Same story as TYPE_MATERIAL above, one
    # lifecycle later: SaveRecordingView sent nothing at all while the upload
    # form's rail promised "Students notified". Its own type rather than
    # SESSION (a recording is not a scheduled class — it has no start time and
    # nothing to join) or MATERIAL (which routes clicks to the handouts
    # screen, not the video).
    TYPE_RECORDING = "RECORDING"

    TYPE_CHOICES = [
        (TYPE_ASSIGNMENT, "Assignment"),
        (TYPE_QUIZ, "Quiz"),
        (TYPE_SESSION, "Live Session"),
        (TYPE_SUBMISSION, "Submission"),
        (TYPE_MATERIAL, "Study Material"),
        (TYPE_RECORDING, "Recording"),
    ]

    # Which dashboard identity this row belongs to.
    AUDIENCE_LEARNER = "LEARNER"
    AUDIENCE_TEACHER = "TEACHER"
    AUDIENCE_CHOICES = [
        (AUDIENCE_LEARNER, "Learner"),
        (AUDIENCE_TEACHER, "Teacher"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="activities",
    )

    # NEW — LEARNER rows point at the exact profile that should see them.
    # NULL means "every learner profile of this account" (legacy rows,
    # or genuinely account-wide announcements).
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="activities",
        null=True,
        blank=True,
    )

    # NEW — identity scope. Backfilled by migration 0003.
    audience = models.CharField(
        max_length=10,
        choices=AUDIENCE_CHOICES,
        default=AUDIENCE_LEARNER,
        db_index=True,
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
            # Feed hot path: (who, which identity, which profile, newest first)
            models.Index(fields=["user", "audience", "learner_profile", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.type} - {self.title}"
