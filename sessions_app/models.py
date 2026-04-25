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


# ===========================================================================
# STUDY GROUPS
# ===========================================================================
# Completely separate tables from PrivateSession so the existing
# private-session flow remains untouched and every query on this feature is
# explicit.  Patterns (UUID PK, LiveKit room_name, active_connections /
# all_left_at auto-expire) mirror PrivateSession so the consumer and
# cleanup-command logic can be reused.


class StudyGroupSession(models.Model):
    """
    A student-initiated study group room.

    Lifecycle:
        scheduled  → live  → completed
                   ↘ cancelled (terminal, settable from scheduled)

    The room becomes joinable inside a window around ``scheduled_time``
    once at least one invitee has accepted.  Duration is enforced
    server-side from the first join (``room_started_at``).
    """

    STATUS_CHOICES = [
        ("scheduled", "Scheduled"),
        ("live", "Live"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("expired", "Expired"),
    ]

    DURATION_CHOICES = [
        (30, "30 minutes"),
        (45, "45 minutes"),
        (60, "1 hour"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Parties ---
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hosted_study_groups",
    )
    # Optional teacher link; if the host invited a teacher, this is the
    # target.  Acceptance is tracked in the StudyGroupInvite row.
    invited_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invited_study_groups",
    )

    # --- Academic scope (mirror how PrivateSession stores subject) ---
    # We keep a FK to the actual Subject so student/teacher pools can be
    # resolved at join-time *and* store the denormalised name for history.
    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.PROTECT,
        related_name="study_groups",
    )
    subject_name = models.CharField(max_length=255)
    course_title = models.CharField(max_length=255, blank=True, default="")

    topic = models.CharField(max_length=255, blank=True, default="")

    # --- Scheduling ---
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(
        choices=DURATION_CHOICES, default=45
    )

    # --- Capacity (host + invitees) ---
    # Max invitees is 20.  Minimum 1 invitee must accept before the room
    # will open.
    max_invitees = models.PositiveIntegerField(default=20)

    # --- Lifecycle ---
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="scheduled"
    )
    cancel_reason = models.TextField(blank=True, default="")

    # --- LiveKit ---
    room_name = models.CharField(max_length=255, blank=True, default="")

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set on first participant join — the hard-duration cutoff is measured
    # from this instant.
    room_started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # --- Idle-expire tracking (identical to PrivateSession's fields) ---
    active_connections = models.IntegerField(default=0)
    all_left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["host", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scheduled_date"]),
        ]

    def __str__(self):
        return f"StudyGroup {self.id} — {self.subject_name} ({self.status})"

    # ---- convenience ----
    @property
    def scheduled_at(self):
        """Combined aware datetime (naive until view-layer makes it aware)."""
        from datetime import datetime
        return datetime.combine(self.scheduled_date, self.scheduled_time)


class StudyGroupInvite(models.Model):
    """
    One row per invited user (student or optional teacher).

    The host is *not* stored here (they're implicit via
    ``StudyGroupSession.host``).  Invitees may be re-invited exactly once
    after declining, enforced by ``decline_count <= 1`` and
    ``reinvited_at``.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("declined", "Declined"),
    ]

    INVITE_ROLE_CHOICES = [
        ("student", "Student"),
        ("teacher", "Teacher"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        StudyGroupSession,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="study_group_invites",
    )
    invite_role = models.CharField(
        max_length=10, choices=INVITE_ROLE_CHOICES, default="student"
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default="pending"
    )
    decline_count = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    reinvited_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "user")
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["session", "status"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.session.id} [{self.status}]"