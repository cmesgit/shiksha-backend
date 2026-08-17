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

    session = models.ForeignKey(
        GroupSession, related_name="remote_grants", on_delete=models.CASCADE
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

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"RC {self.controller_id}->{self.target_id} on {self.session_id} [{self.status}]"
