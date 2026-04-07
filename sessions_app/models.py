import uuid
from django.conf import settings
from django.db import models


class PrivateSession(models.Model):
    """
    Core model for 1-on-1 or small-group private tutoring sessions.
    Tracks the full lifecycle: request → approval → live → completed.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("declined", "Declined"),
        ("needs_reconfirmation", "Needs Reconfirmation"),
        ("ongoing", "Ongoing"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("expired", "Expired"),
        ("withdrawn", "Withdrawn"),
        ("teacher_no_show", "Teacher No Show"),
        ("student_no_show", "Student No Show"),
    ]

    SESSION_TYPE_CHOICES = [
        ("one_on_one", "One on One"),
        ("group", "Group"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Parties (UUID FK to accounts.User) ---
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="taught_private_sessions",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="requested_private_sessions",
    )

    # --- Scheduling ---
    subject = models.CharField(max_length=255)
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=60)

    # --- Rescheduling (teacher-proposed) ---
    rescheduled_date = models.DateField(null=True, blank=True)
    rescheduled_time = models.TimeField(null=True, blank=True)
    reschedule_reason = models.TextField(blank=True, default="")

    # --- Session metadata ---
    session_type = models.CharField(
        max_length=20, choices=SESSION_TYPE_CHOICES, default="one_on_one"
    )
    group_strength = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending")
    notes = models.TextField(blank=True, default="")
    decline_reason = models.TextField(blank=True, default="")
    cancel_reason = models.TextField(blank=True, default="")

    # --- LiveKit (reuses existing livestream infrastructure) ---
    room_name = models.CharField(max_length=255, blank=True, default="")

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # --- Auto-expire tracking ---
    # Number of active WebSocket connections in this room
    active_connections = models.IntegerField(default=0)
    # When the last participant left (null = someone is still connected)
    all_left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["teacher", "status"]),
            models.Index(fields=["requested_by", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scheduled_date"]),
        ]

    def __str__(self):
        return f"PrivateSession {self.id} — {self.subject} ({self.status})"


class SessionParticipant(models.Model):
    """
    Tracks additional students in a group private session.
    The requesting student is always implicitly a participant.
    """

    ROLE_CHOICES = [
        ("student", "Student"),
        ("observer", "Observer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PrivateSession, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="private_session_participations",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="student")
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user} in {self.session.id}"


class SessionRescheduleHistory(models.Model):
    """Audit log for every reschedule proposal on a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PrivateSession, on_delete=models.CASCADE, related_name="reschedule_history"
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE
    )
    original_date = models.DateField()
    original_time = models.TimeField()
    proposed_date = models.DateField()
    proposed_time = models.TimeField()
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reschedule for {self.session.id} on {self.created_at}"


class ChatMessage(models.Model):
    """
    Persistent chat messages for private sessions.
    Messages persist until the session ends or is cancelled.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PrivateSession, on_delete=models.CASCADE, related_name="chat_messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="private_session_messages",
    )
    sender_name = models.CharField(max_length=255)
    sender_role = models.CharField(max_length=20, default="student")  # "teacher" or "student"
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self):
        return f"Chat in {self.session.id} by {self.sender_name} at {self.created_at}"