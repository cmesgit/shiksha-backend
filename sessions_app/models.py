import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


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
    # Dual-keying (same convention as enrollments / quiz attempts / assignment
    # submissions): `requested_by` is the paying/audit ACCOUNT, `learner_profile`
    # is WHICH learner on that account the session is for. Nullable for legacy
    # rows created before per-profile attribution — backfilled by
    # `manage.py backfill_session_profiles`. All student-side reads/writes scope
    # by this, and teacher-facing rows display the profile's name so two children
    # on one account are distinguishable.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="private_sessions",
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
            models.Index(fields=["learner_profile", "status"]),
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

    status = models.CharField(
        max_length=10,
        choices=[
            ("pending", "Pending"),
            ("accepted", "Accepted"),
            ("declined", "Declined"),
        ],
        default="pending",
    )
    
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user} in {self.session.id}"


class PrivateSessionAttendance(models.Model):
    """Per-user attendance ROLLUP for a PrivateSession (teacher AND the
    requesting student — not just SessionParticipant's "additional students"
    concern). Populated from the LiveKit webhook the same way livestream/
    GroupSession already are; previously PrivateSession had no per-participant
    join/leave/duration tracking at all.

    Mirrors GroupSessionAttendance exactly — see
    sessions_app/services/private_attendance.py.
    """
    session = models.ForeignKey(
        PrivateSession,
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


class PrivateSessionAttendanceInterval(models.Model):
    """Append-only, one row per join→leave cycle — mirrors
    GroupSessionAttendanceInterval exactly."""
    session = models.ForeignKey(
        PrivateSession,
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


class PrivateSessionReview(models.Model):
    """A participant's post-class rating for one PrivateSession.

    One review per (session, user) — resubmitting overwrites via
    update_or_create rather than erroring. Mirrors livestream.SessionReview.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PrivateSession, on_delete=models.CASCADE, related_name="reviews"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
    )
    rating = models.PositiveSmallIntegerField(
        choices=[(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]
    )
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user_id} rated {self.session_id}: {self.rating}"


class PrivateSessionNote(models.Model):
    """A participant's private notes for one PrivateSession.

    Private per (session, user) — mirrors livestream.SessionNote. Distinct
    from PrivateSession.notes (a single shared free-text field on the
    session itself, unrelated to this per-user in-call scratchpad).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        PrivateSession, on_delete=models.CASCADE, related_name="participant_notes"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
    )
    content = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user_id} notes on {self.session_id}"


class PrivateSessionFile(models.Model):
    """A file shared inside a PrivateSession — mirrors GroupSession's
    SessionFile exactly (same purge command sweeps both; see
    management/commands/purge_expired_session_files.py)."""

    session = models.ForeignKey(
        PrivateSession, related_name="files", on_delete=models.CASCADE
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="private_session_files", on_delete=models.CASCADE
    )
    file = models.FileField(upload_to="session_files/%Y/%m/%d/")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    saved_to_course = models.BooleanField(
        default=False,
        help_text="Survives the purge — copied into course materials.",
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.original_name} ({self.session_id})"


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


# ---------------------------------------------------------------------------
# Group-session chat
#
# A separate table from ChatMessage rather than a generic FK because:
#   1. ChatMessage has a hard FK to PrivateSession in the DB and changing
#      that to nullable would touch a lot of existing query paths.
#   2. Per-session-type retention rules differ — group-session messages are
#      deleted from the DB the moment the room ends (per product spec
#      "the chat is stored in the live room until it closes, then it can
#      be deleted from the database only after the room is ended"),
#      whereas private-session messages persist with the row until the
#      session is fully cleaned up.
# Cleanup happens in group_session_views._end_group_session_internal which
# bulk-deletes GroupSessionChatMessage.objects.filter(session=...).
# ---------------------------------------------------------------------------
class GroupSessionChatMessage(models.Model):
    """Chat messages scoped to a single live GroupSession."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "GroupSession",
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_session_messages",
    )
    sender_name = models.CharField(max_length=255)
    # "host" / "teacher" / "student" — the in-room badge shown to peers.
    sender_role = models.CharField(max_length=20, default="student")
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self):
        return f"GSChat in {self.session_id} by {self.sender_name}"


# ===========================================================================
# GROUP SESSIONS
# ===========================================================================
# Completely separate tables from PrivateSession so the existing
# private-session flow remains untouched and every query on this feature is
# explicit.  Patterns (UUID PK, LiveKit room_name, active_connections /
# all_left_at auto-expire) mirror PrivateSession so the consumer and
# cleanup-command logic can be reused.


class GroupSession(models.Model):
    """
    A student-initiated group session room.

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
        # Instant meetings default to a longer window since they're not
        # pre-scheduled. The duration is still enforced from room_started_at.
        (180, "3 hours"),
    ]

    # Scheduled (Create Group Session flow) vs Instant (Create Instant Meeting).
    # Instant meetings skip the invite-and-accept gating entirely — the room is
    # opened at the moment of creation and joinable by anyone with the link
    # who passes the auth/paid checks.
    SESSION_TYPE_CHOICES = [
        ("scheduled", "Scheduled"),
        ("instant", "Instant"),
    ]

    # Admit mode controls how non-host participants enter the room.
    #   open  — anyone with a valid token joins directly (default, current behavior)
    #   lobby — non-hosts go through a host-approval queue (Google-Meet-style)
    ADMIT_MODE_CHOICES = [
        ("open", "Allow anyone"),
        ("lobby", "Admit Users"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human-readable short code shown in the meeting-ready dialog and used
    # as the shareable suffix in the "Add others" copyable link. Generated
    # at create-time. Distinct from the UUID PK so the URL is friendly.
    short_code = models.CharField(
        max_length=20, unique=True, blank=True, default="",
        help_text="Short shareable code (e.g. 'zfk-pbmc-rxd').",
    )

    # --- Parties ---
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hosted_group_sessions",
    )
    # Optional teacher link; if the host invited a teacher, this is the
    # target.  Acceptance is tracked in the GroupSessionInvite row.
    invited_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invited_group_sessions",
    )

    # --- Academic scope (mirror how PrivateSession stores subject) ---
    # We keep a FK to the actual Subject so student/teacher pools can be
    # resolved at join-time *and* store the denormalised name for history.
    # Subject is OPTIONAL for instant meetings (they're not tied to a course),
    # but REQUIRED for scheduled group sessions — enforced in the view layer.
    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="group_sessions",
    )
    subject_name = models.CharField(max_length=255, blank=True, default="")
    course_title = models.CharField(max_length=255, blank=True, default="")

    # Type discriminator + admit gating.
    session_type = models.CharField(
        max_length=20, choices=SESSION_TYPE_CHOICES, default="scheduled"
    )
    admit_mode = models.CharField(
        max_length=10, choices=ADMIT_MODE_CHOICES, default="open"
    )

    topic = models.CharField(max_length=255, blank=True, default="")

    # --- Scheduling ---
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(
        choices=DURATION_CHOICES, default=45
    )

    # --- Capacity (host + invitees) ---
    # Max invitees is 50 (bumped from 20 — room cap is host + invitees so
    # 51 total participants per session). Minimum 1 invitee must accept
    # before the room will open. Note: LiveKit Cloud free tier caps rooms
    # at ~25 concurrent participants; self-hosted has no implicit cap.
    max_invitees = models.PositiveIntegerField(default=50)

    # --- Lifecycle ---
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="scheduled"
    )
    cancel_reason = models.TextField(blank=True, default="")

    # --- LiveKit ---
    # null=True (not just blank) so pre-live rows can share the "not set yet"
    # state without violating uniqueness — many sessions never go live and
    # would otherwise collide on "". Set once, at go-live, to
    # f"group_session_{id}" (inherently unique).
    room_name = models.CharField(
        max_length=255, null=True, blank=True, default=None, unique=True
    )

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

    # --- Per-user hide (History "Clear" UX) ---
    # Any user in this set won't see this session in their History tab. The
    # underlying session row is preserved — the host and other participants
    # still see it, analytics/audit trails still work. This is a soft
    # delete scoped to the requesting user, deliberately separate from the
    # ``cancelled`` lifecycle state.
    hidden_for = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="hidden_group_sessions",
        blank=True,
    )

    # --- Live-session rules (sessions_app/live_rules.py) ---
    # How many times the host has used the "extend" action (screen 07). Each
    # extension adds GlobalSettings.live_host_extension_minutes, bounded by
    # live_max_session_minutes — see live_rules.cap_ends_at().
    extensions_used = models.PositiveSmallIntegerField(default=0)
    # Cached absolute end-of-room instant, recomputed on join/extend so
    # clients can render a countdown without recomputing the formula
    # themselves. Nullable — unset until the room actually goes live.
    cap_ends_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["host", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scheduled_date"]),
        ]

    def __str__(self):
        return f"GroupSession {self.id} — {self.subject_name} ({self.status})"

    # ---- convenience ----
    @property
    def scheduled_at(self):
        """Combined aware datetime (naive until view-layer makes it aware)."""
        from datetime import datetime
        return datetime.combine(self.scheduled_date, self.scheduled_time)


class GroupSessionInvite(models.Model):
    """
    One row per invited user (student or optional teacher).

    The host is *not* stored here (they're implicit via
    ``GroupSession.host``).  Invitees may be re-invited exactly once
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
        GroupSession,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_session_invites",
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


class GroupSessionGuestSession(models.Model):
    """
    Anchors a non-entitled guest's 15-minute free-trial clock to their FIRST
    join, so a page refresh / reconnect doesn't reset it back to a fresh 15
    minutes. One row per (session, user) — created lazily on first non-host,
    non-entitled join in ``join_group_session``.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        GroupSession,
        on_delete=models.CASCADE,
        related_name="guest_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_session_guest_sessions",
    )
    first_joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"guest {self.user} → {self.session.id} since {self.first_joined_at}"


class GroupSessionJoinRequest(models.Model):
    """
    A guest's "knock to join" request when ``GroupSession.admit_mode ==
    'lobby'``. Separate from ``GroupSessionInvite``: invites are host-initiated
    and pre-arranged (re-invite semantics, decline tracking); this is
    guest-initiated and ad-hoc, and applies to instant meetings that have no
    invite rows at all.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("admitted", "Admitted"),
        ("denied", "Denied"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        GroupSession,
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_session_join_requests",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    deny_message = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ("session", "user")
        indexes = [
            models.Index(fields=["session", "status"]),
        ]

    def __str__(self):
        return f"{self.user} knocking on {self.session.id} [{self.status}]"


class InstantCollaborationGuest(models.Model):
    """A temporary, room-scoped visitor; never an account or group invite."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        GroupSession, on_delete=models.CASCADE, related_name="instant_guests"
    )
    name = models.CharField(max_length=80)
    status = models.CharField(
        max_length=10,
        choices=[("pending", "Pending"), ("admitted", "Admitted"),
                 ("denied", "Denied"), ("removed", "Removed")],
        default="pending",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["session", "status", "last_seen_at"])]


class GroupSessionAttendance(models.Model):
    session = models.ForeignKey(
        GroupSession,
        on_delete=models.CASCADE,
        related_name="attendances",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    # ── ROLLUP semantics ──
    # joined_at = FIRST join, left_at = LAST leave, total_seconds = summed watch
    # time across every join/leave cycle (see GroupSessionAttendanceInterval for
    # the append-only per-cycle rows). unique_together keeps exactly one rollup
    # row per (session, user); rejoins no longer overwrite prior attendance —
    # they extend total_seconds instead.
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


class GroupSessionNote(models.Model):
    """A participant's private notes for one GroupSession.

    Private per (session, user) — mirrors livestream.SessionNote /
    PrivateSessionNote.

    NOTE: an earlier revision of this docstring said "no review-model
    counterpart: group sessions never show a post-call review modal per the
    design spec." That was true through the Phase 1-4 live-session work but
    is superseded by design_handoff_live_sessions Phase 5 (screen 09, the
    post-session summary), which does add a review form — see
    ``GroupSessionReview`` below.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        GroupSession, on_delete=models.CASCADE, related_name="notes"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
    )
    content = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user_id} notes on {self.session_id}"


class GroupSessionReview(models.Model):
    """A participant's post-session rating for one GroupSession (design
    screen 09 — the summary page's "How was the session?" card).

    Mirrors ``PrivateSessionReview`` exactly (same rating scale, same
    update_or_create-on-resubmit semantics via the unique constraint) — this
    is genuinely NEW, added for design_handoff_live_sessions Phase 5. No
    such model or endpoint existed for GroupSession before this; the
    handoff's 01-FLOW.md assumed one already existed ("posts to the
    existing SessionReview endpoint"), but the only real ``SessionReview``
    in this codebase belongs to the unrelated ``livestream`` app (LiveSession,
    not GroupSession), and the only other reviewable session type
    (PrivateSession) has its own, differently-named model. Kept additive:
    no existing model/endpoint changed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        GroupSession, on_delete=models.CASCADE, related_name="reviews"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
    )
    rating = models.PositiveSmallIntegerField(
        choices=[(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]
    )
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("session", "user")

    def __str__(self):
        return f"{self.user_id} rated {self.session_id}: {self.rating}"


class GroupSessionAttendanceInterval(models.Model):
    """Append-only, one row per join→leave cycle.

    The authoritative attendance record. `GroupSessionAttendance` is a derived
    rollup over these. A user who disconnects and rejoins produces multiple
    rows, so reconnect history, total watch time, and true presence intervals
    are all recoverable — which the single-interval rollup alone can't do.
    `closed_by_reconcile` marks rows we closed defensively (missed
    participant_left webhook) rather than a real leave.
    """
    session = models.ForeignKey(
        GroupSession,
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


# ===========================================================================
# LIVE SESSIONS — Phase 1 additions (file sharing, remote control)
# ===========================================================================
# Design handoff: design_handoff_live_sessions/02-BACKEND.md + backend/
# sessions_app/{live_rules,live_files_views,remote_control_views,
# models_additions}.py.
#
# Deviation from the handoff worth flagging: its live_files_views.py and
# remote_control_views.py both assume a ``session.participants`` reverse
# relation (rows with ``user``/``left_at``/``is_sharing_screen``) to answer
# "is this user currently in the room" and "are they sharing their screen".
# No such model exists anywhere in this codebase today — GroupSession only
# tracks an aggregate ``active_connections`` counter (consumers.py), and the
# two existing per-user tables (GroupSessionAttendance, which rolls up
# first-join/last-leave across the whole lifetime, and
# GroupSessionAttendanceInterval above, an append-only join/leave audit log)
# are both driven by the LiveKit webhook pipeline and don't answer "right
# now." GroupSessionParticipant below is a NEW model that fills that gap: one
# row per (session, user), upserted with ``left_at=None`` in
# group_session_views.join_group_session (the one call every real client
# makes before it ever opens the LiveKit connection) and closed out
# (``left_at`` set) when the room itself ends. This is honest, not
# aspirational: leaving a room WITHOUT the room ending (e.g. closing the tab
# mid-session) is not yet wired back to this table — the WS consumer
# (GroupSessionChatConsumer) never resolves an authenticated user identity
# today, only an anonymous connection counter — so "in_room" here is a
# best-effort join-time signal, not a live presence heartbeat. Likewise
# ``is_sharing_screen`` has no producer yet: there is no LiveKit
# track-published webhook handler for group sessions, so nothing flips this
# flag to True today. Both are flagged as open follow-up work, not silently
# faked.
class GroupSessionParticipant(models.Model):
    """Best-effort "who is in this room right now" — see the module note
    above for exactly what this can and can't promise in Phase 1."""

    session = models.ForeignKey(
        GroupSession, related_name="participants", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="group_session_participations",
        on_delete=models.CASCADE,
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    is_sharing_screen = models.BooleanField(default=False)

    class Meta:
        unique_together = ("session", "user")
        indexes = [
            models.Index(fields=["session", "left_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} in {self.session_id} (left_at={self.left_at})"


class SessionFile(models.Model):
    """A file shared inside a live session. Purged after the retention window
    (see management/commands/purge_expired_session_files.py) unless a
    learner explicitly saved it to their course."""

    session = models.ForeignKey(
        GroupSession, related_name="files", on_delete=models.CASCADE
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="session_files", on_delete=models.CASCADE
    )
    file = models.FileField(upload_to="session_files/%Y/%m/%d/")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    saved_to_course = models.BooleanField(
        default=False,
        help_text="Survives the purge — copied into course materials.",
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.original_name} ({self.session_id})"


class RemoteControlGrant(models.Model):
    """Audit row for one teacher→student screen-control grant. See
    remote_control_views.py's module docstring for the open question about
    what actually drives the input on the target's side — this model only
    covers authorisation + audit, not the input transport itself."""

    STATUS_REQUESTED = "requested"
    STATUS_ACTIVE = "active"
    STATUS_DECLINED = "declined"
    STATUS_REVOKED = "revoked"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_EXPIRED, "Expired"),
    ]

    # Nullable as of RC-005 so a non-group SessionRef can be represented. Every
    # legacy row has it set, and the exactly-one-session check constraint stops
    # a row from having none. on_delete stays CASCADE — see the divergence note
    # in evidence/collaboration-schema.md.
    session = models.ForeignKey(
        GroupSession, null=True, blank=True,
        related_name="remote_grants", on_delete=models.CASCADE,
    )
    controller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="rc_controlling",
        on_delete=models.CASCADE,
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="rc_targeted",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_REQUESTED
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    granted_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # target|controller|system — free-text audit tag, not an FK.
    ended_by = models.CharField(max_length=16, blank=True)

    # ── Shiksha Remote v1 (RC-005) ────────────────────────────────────────
    # Everything below is ADDITIVE and nullable. Rows written by the legacy
    # teacher→student path keep schema_version=0 and never gain these fields;
    # `remote_control_views.py` is untouched by this migration and still
    # writes legacy rows. See docs/architecture/shiksha-remote/
    # database-specification.md — "extend existing Grant, do not duplicate".
    SCHEMA_LEGACY = 0
    SCHEMA_V1 = 1

    # The protocol's nine states. The legacy five are a subset, so existing
    # rows stay valid; `paused`, `approved`, `completed` and `failed` are new.
    STATUS_APPROVED = "approved"
    STATUS_PAUSED = "paused"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    V1_STATUS_CHOICES = STATUS_CHOICES + [
        (STATUS_APPROVED, "Approved"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]
    NONTERMINAL_STATUSES = (
        STATUS_REQUESTED, STATUS_APPROVED, STATUS_ACTIVE, STATUS_PAUSED,
    )
    # The states that reserve the one-grant-per-device slot.
    DEVICE_RESERVING_STATUSES = (STATUS_APPROVED, STATUS_ACTIVE, STATUS_PAUSED)

    schema_version = models.SmallIntegerField(default=SCHEMA_LEGACY)
    public_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    # Exactly one session FK is non-null, enforced by a check constraint.
    # `session` (GroupSession) keeps CASCADE deliberately — see the divergence
    # note in evidence/collaboration-schema.md. The three new FKs use PROTECT
    # as the specification requires, because nothing depends on them yet.
    private_session = models.ForeignKey(
        "sessions_app.PrivateSession", null=True, blank=True,
        related_name="remote_grants", on_delete=models.PROTECT,
    )
    live_session = models.ForeignKey(
        "livestream.LiveSession", null=True, blank=True,
        related_name="remote_grants", on_delete=models.PROTECT,
    )
    skill_session = models.ForeignKey(
        "skills.SkillSession", null=True, blank=True,
        related_name="remote_grants", on_delete=models.PROTECT,
    )

    device = models.ForeignKey(
        "sessions_app.PairedDevice", null=True, blank=True,
        related_name="grants", on_delete=models.PROTECT,
    )

    # Server-derived auth context snapshots. Never read from a request body —
    # api-contracts.md: "Never infer authority from role sent in body."
    controller_context = models.JSONField(null=True, blank=True)
    target_context = models.JSONField(null=True, blank=True)

    controller_binding_id = models.UUIDField(null=True, blank=True)
    host_binding_id = models.UUIDField(null=True, blank=True)

    room_name = models.CharField(max_length=255, blank=True)
    controller_identity = models.CharField(max_length=255, blank=True)
    host_identity = models.CharField(max_length=255, blank=True)
    companion_identity = models.CharField(max_length=255, blank=True)
    controller_sid = models.CharField(max_length=128, blank=True)
    host_sid = models.CharField(max_length=128, blank=True)
    companion_sid = models.CharField(max_length=128, blank=True)
    track_sid = models.CharField(max_length=128, blank=True)

    # Sorted, unique, drawn from the five-item allowlist. No default grants
    # input — an empty list is the safe state.
    capabilities = models.JSONField(default=list, blank=True)
    display_descriptor = models.JSONField(null=True, blank=True)
    capture_generation = models.PositiveIntegerField(null=True, blank=True)

    epoch = models.UUIDField(null=True, blank=True)
    version = models.BigIntegerField(default=1)

    request_expires_at = models.DateTimeField(null=True, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True)
    resume_intent_expires_at = models.DateTimeField(null=True, blank=True)

    # One-use activation/resume challenge. The nonce itself is never stored.
    consent_nonce_hash = models.BinaryField(max_length=32, null=True, blank=True)
    reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            # Exactly one session FK, for every row including legacy ones.
            models.CheckConstraint(
                name="rc_grant_exactly_one_session",
                condition=(
                    models.Q(session__isnull=False, private_session__isnull=True,
                             live_session__isnull=True, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=False,
                               live_session__isnull=True, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=True,
                               live_session__isnull=False, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=True,
                               live_session__isnull=True, skill_session__isnull=False)
                ),
            ),
            # A v1 row must carry its device and both bindings. Legacy rows are
            # exempt, which is what lets this ship without a backfill.
            models.CheckConstraint(
                name="rc_grant_v1_requires_device_and_bindings",
                condition=(
                    models.Q(schema_version=0)
                    | models.Q(device__isnull=False,
                               controller_binding_id__isnull=False,
                               host_binding_id__isnull=False,
                               public_id__isnull=False)
                ),
            ),
            # Controlling your own account is denied in MVP — same-account
            # profiles are distinct contexts and the identity is ambiguous.
            models.CheckConstraint(
                name="rc_grant_controller_is_not_target",
                condition=~models.Q(controller=models.F("target")),
            ),
            # One live grant per device, across every room. Partial so legacy
            # rows and terminal rows don't occupy the slot. No NOW() here —
            # expiry is transitioned under lock before an insert, never
            # inferred by the index (database-specification.md).
            models.UniqueConstraint(
                fields=["device"],
                condition=models.Q(
                    schema_version=1,
                    status__in=["approved", "active", "paused"],
                ),
                name="rc_grant_one_live_grant_per_device",
            ),
            # Stops a controller flooding one device with pending requests.
            models.UniqueConstraint(
                fields=["controller", "device"],
                condition=models.Q(schema_version=1, status="requested"),
                name="rc_grant_one_pending_request_per_controller_device",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "status"], name="rc_grant_device_status"),
            models.Index(fields=["controller", "-requested_at"], name="rc_grant_ctrl_time"),
            models.Index(fields=["target", "status", "-requested_at"], name="rc_grant_tgt_time"),
            models.Index(fields=["status", "expires_at"], name="rc_grant_status_expiry"),
            models.Index(fields=["status", "request_expires_at"], name="rc_grant_status_req_exp"),
        ]

    def __str__(self):
        return f"RC {self.controller_id}->{self.target_id} on {self.session_id} [{self.status}]"

    @property
    def is_v1(self):
        return self.schema_version == self.SCHEMA_V1

    def session_ref(self):
        """The one non-null session FK, as the wire's `SessionRef` shape.

        Returns None for a row that somehow has none — the check constraint
        makes that unreachable in the database, but this is read on paths
        where failing closed beats raising.
        """
        for kind, value in (
            ("group", self.session_id), ("private", self.private_session_id),
            ("live", self.live_session_id), ("skill", self.skill_session_id),
        ):
            if value is not None:
                return {"kind": kind, "id": str(value)}
        return None


class PairedDevice(models.Model):
    """A desktop the owner has paired, and the credential it authenticates with.

    Not an account and not a login: a paired device is a subordinate credential
    that can only ever act for `owner`, and only while `status == "paired"`.
    Revoking it must stop WSS connection, capture publication and lease
    issuance — holding a valid token is not sufficient.

    `name` is display metadata the user typed. It is escaped on render and is
    never authority; two devices may share a name.
    """

    STATUS_PENDING = "pending"
    STATUS_PAIRED = "paired"
    STATUS_REVOKED = "revoked"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PAIRED, "Paired"),
        (STATUS_REVOKED, "Revoked"),
    ]

    MAX_PER_ACCOUNT = 5

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="paired_devices",
        on_delete=models.PROTECT,
    )
    paired_context = models.JSONField(null=True, blank=True)

    name = models.CharField(max_length=80)
    platform = models.CharField(max_length=32)
    arch = models.CharField(max_length=16)
    app_version = models.CharField(max_length=32, blank=True)
    protocol_min = models.SmallIntegerField(default=1)
    protocol_max = models.SmallIntegerField(default=1)

    public_jwk = models.JSONField()
    key_thumbprint = models.CharField(max_length=64, unique=True)
    # Hashes only. The credential itself never lands in a column.
    credential_hash = models.BinaryField(max_length=32, null=True, blank=True)
    # Kept solely so a replayed old refresh token is detectable as reuse.
    previous_credential_hash = models.BinaryField(max_length=32, null=True, blank=True)
    credential_expires_at = models.DateTimeField(null=True, blank=True)
    credential_version = models.BigIntegerField(default=1)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paired_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Coarse on purpose — persisted at most every 5 minutes. Real liveness is
    # a Redis record with a 6-second TTL; this column must never gate a lease.
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"], name="paired_device_owner_status"),
            models.Index(fields=["credential_expires_at"], name="paired_device_cred_exp"),
        ]

    def __str__(self):
        return f"{self.name} ({self.platform}/{self.arch}) [{self.status}]"

    @property
    def is_usable(self):
        """Paired and not revoked. Deliberately does not consult last_seen_at."""
        return self.status == self.STATUS_PAIRED


class FileTransfer(models.Model):
    """One file offered to exactly one recipient.

    `recipient` is never null and never means "everyone" — course-wide
    distribution keeps using the existing course models and their ACLs. The
    durable-storage columns are deliberately absent until RC-019; this table
    carries metadata only and no blob.
    """

    MODE_REALTIME = "realtime"
    MODE_DURABLE = "durable"
    MODE_CHOICES = [(MODE_REALTIME, "Realtime"), (MODE_DURABLE, "Durable")]

    STATE_OFFERED = "offered"
    STATE_ACCEPTED = "accepted"
    STATE_REJECTED = "rejected"
    STATE_SENDING = "sending"
    STATE_RECEIVED = "received"
    STATE_FAILED = "failed"
    STATE_CANCELLED = "cancelled"
    STATE_EXPIRED = "expired"
    STATE_CHOICES = [
        (STATE_OFFERED, "Offered"), (STATE_ACCEPTED, "Accepted"),
        (STATE_REJECTED, "Rejected"), (STATE_SENDING, "Sending"),
        (STATE_RECEIVED, "Received"), (STATE_FAILED, "Failed"),
        (STATE_CANCELLED, "Cancelled"), (STATE_EXPIRED, "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session = models.ForeignKey(
        GroupSession, null=True, blank=True,
        related_name="file_transfers", on_delete=models.PROTECT,
    )
    private_session = models.ForeignKey(
        "sessions_app.PrivateSession", null=True, blank=True,
        related_name="file_transfers", on_delete=models.PROTECT,
    )
    live_session = models.ForeignKey(
        "livestream.LiveSession", null=True, blank=True,
        related_name="file_transfers", on_delete=models.PROTECT,
    )
    skill_session = models.ForeignKey(
        "skills.SkillSession", null=True, blank=True,
        related_name="file_transfers", on_delete=models.PROTECT,
    )

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="file_transfers_sent",
        on_delete=models.PROTECT,
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="file_transfers_received",
        on_delete=models.PROTECT,
    )
    sender_context = models.JSONField(null=True, blank=True)
    recipient_context = models.JSONField(null=True, blank=True)
    sender_binding_id = models.UUIDField(null=True, blank=True)
    recipient_binding_id = models.UUIDField(null=True, blank=True)
    sender_identity = models.CharField(max_length=255, blank=True)
    recipient_identity = models.CharField(max_length=255, blank=True)
    sender_sid = models.CharField(max_length=128, blank=True)
    recipient_sid = models.CharField(max_length=128, blank=True)

    mode = models.CharField(max_length=12, choices=MODE_CHOICES)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_OFFERED)
    version = models.BigIntegerField(default=1)

    name = models.CharField(max_length=255)
    declared_mime = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    stream_id = models.UUIDField(null=True, blank=True, unique=True)
    attempt = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    offer_expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                name="file_transfer_exactly_one_session",
                condition=(
                    models.Q(session__isnull=False, private_session__isnull=True,
                             live_session__isnull=True, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=False,
                               live_session__isnull=True, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=True,
                               live_session__isnull=False, skill_session__isnull=True)
                    | models.Q(session__isnull=True, private_session__isnull=True,
                               live_session__isnull=True, skill_session__isnull=False)
                ),
            ),
            models.CheckConstraint(
                name="file_transfer_sender_is_not_recipient",
                condition=~models.Q(sender=models.F("recipient")),
            ),
        ]
        indexes = [
            models.Index(fields=["sender", "state", "-created_at"], name="transfer_sender_state"),
            models.Index(fields=["recipient", "state", "-created_at"], name="transfer_recip_state"),
            models.Index(fields=["state", "expires_at"], name="transfer_state_expiry"),
        ]

    def __str__(self):
        return f"Transfer {self.name} {self.sender_id}->{self.recipient_id} [{self.state}]"


class CollaborationAuditEvent(models.Model):
    """Append-only record of consent and device-lifecycle transitions.

    What it must never contain: keystrokes, typed text, pointer payloads,
    screen frames, file contents, filenames, bearer tokens or DPoP secrets.
    `metadata` is for allowlisted counts, capability names and app versions.

    This is an audit trail, not a tamper-proof log — a database administrator
    can still alter it. Do not describe it as immutable to users.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now)

    # SET_NULL, not CASCADE: deleting an account must not erase the record
    # that something happened.
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        related_name="collaboration_audit_events", on_delete=models.SET_NULL,
    )
    actor_device = models.ForeignKey(
        PairedDevice, null=True, blank=True,
        related_name="audit_events", on_delete=models.SET_NULL,
    )
    actor_context = models.JSONField(null=True, blank=True)

    event_type = models.CharField(max_length=64)
    resource_type = models.CharField(max_length=24)
    resource_id = models.CharField(max_length=64)
    resource_version = models.BigIntegerField()
    session_kind = models.CharField(max_length=16, blank=True)
    session_id = models.UUIDField(null=True, blank=True)
    reason = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [
            # One row per (resource, version, event) — makes the audit write
            # idempotent so a retried transaction cannot double-log.
            models.UniqueConstraint(
                fields=["resource_type", "resource_id", "resource_version", "event_type"],
                name="audit_one_row_per_resource_version_event",
            ),
        ]
        indexes = [
            models.Index(fields=["-occurred_at"], name="audit_time"),
            models.Index(fields=["resource_type", "resource_id", "-occurred_at"],
                         name="audit_resource_time"),
            models.Index(fields=["actor_user", "-occurred_at"], name="audit_actor_time"),
        ]

    def __str__(self):
        return f"{self.event_type} {self.resource_type}:{self.resource_id} v{self.resource_version}"

