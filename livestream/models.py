import uuid
from datetime import timedelta

from django.db import models
from django.conf import settings
from courses.models_chapter_tags import (
    chapter_note_field,
    no_specific_chapter_field,
)


class LiveSession(models.Model):
    # 🔥 FULL STATE SYSTEM
    STATUS_SCHEDULED = "SCHEDULED"
    STATUS_WAITING = "WAITING_FOR_TEACHER"
    STATUS_LIVE = "LIVE"
    STATUS_PAUSED = "PAUSED"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_RECONNECTING = "RECONNECTING"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_WAITING, "Waiting for Teacher"),
        (STATUS_LIVE, "Live"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_RECONNECTING, "Reconnecting"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 🔥 CORE FIELD (DO NOT REMOVE)
    # Meaning: "last time teacher disconnected (uncertain state)"
    teacher_left_at = models.DateTimeField(null=True, blank=True)

    # 🔥 OPTIONAL BUT IMPORTANT (future-proofing)
    last_activity_at = models.DateTimeField(null=True, blank=True)

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

    # Delivery scope. NULL = visible to every batch of the course (legacy /
    # course-wide); set = this batch's timetable entry only. New sessions
    # should always carry a batch (enforced in the serializer, not the DB,
    # so legacy rows stay valid). SET_NULL: deleting a batch demotes its
    # sessions to course-wide instead of destroying them.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="live_sessions",
    )

    title = models.CharField(max_length=255)

    # --- Flexible chapter tagging (courses.models_chapter_tags) ---
    # The rich multi-chapter placement lives in ContentChapterTag, keyed on
    # (content_type, object_id). These two are the scalar companions; see
    # courses/models_chapter_tags.py for what each one means and why
    # no_specific_chapter is not the same state as "no tags".
    chapter_note = chapter_note_field()
    no_specific_chapter = no_specific_chapter_field()
    description = models.TextField(blank=True)

    # 🧠 PLANNING LAYER ONLY
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    # Teacher-granted extension. end_time is the PLANNED end; this is the
    # agreed one when a class runs long, set via the extend endpoint.
    extended_until = models.DateTimeField(null=True, blank=True)

    # Shared by every class generated from one recurring pattern, so a
    # 6-month timetable stays addressable as a unit instead of 50 unrelated
    # rows. NULL for one-off sessions, including every session created before
    # recurring scheduling existed.
    series_id = models.UUIDField(null=True, blank=True, db_index=True)

    room_name = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_live_sessions",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # ── Actuals (webhook-stamped) ──
    # start_time / end_time are the PLANNED slot. These capture what really
    # happened: actual_started_at is stamped on the first room_started /
    # teacher join; actual_ended_at on room_finished. Enables true live-duration
    # analytics independent of the schedule.
    actual_started_at = models.DateTimeField(null=True, blank=True)
    actual_ended_at = models.DateTimeField(null=True, blank=True)

    # Highest concurrent-viewer count observed (finalized from viewer samples /
    # LiveKit participant polls). 0 until the first sample lands.
    peak_viewers = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["start_time"]
        indexes = [
            models.Index(fields=["course"]),
            models.Index(fields=["subject"]),
            models.Index(fields=["start_time"]),
            models.Index(fields=["status"]),
            models.Index(fields=["teacher_left_at"]),
            models.Index(fields=["course", "start_time"]),
            models.Index(fields=["subject", "start_time"]),
            models.Index(fields=["batch", "start_time"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.subject.name})"

    # ✅ UI / analytics only
    def duration(self):
        return self.end_time - self.start_time

    # How long a class may overrun its planned end_time before the system
    # treats it as genuinely over. Real teaching overruns — a 10:00-11:00
    # class finishing at 11:10 is routine — and end_time was being enforced
    # as a hard kill in three places at once (the join gate, computed_status
    # and the 5-min sweep). The visible failure: at 11:00 the class flipped
    # to COMPLETED while the LiveKit room was still open, so the lesson
    # carried on for everyone already connected, but any student whose wifi
    # blipped got "Session has ended." and could not get back in.
    LIVE_GRACE = timedelta(minutes=30)

    # Cap on a single extension, so a forgotten class cannot hold a room (and
    # bill LiveKit minutes) indefinitely.
    MAX_EXTENSION = timedelta(hours=3)

    @property
    def was_missed(self):
        """A class that is over and never actually happened.

        DERIVED, deliberately — not a STATUS_MISSED value. Sixteen separate
        places treat COMPLETED as terminal (the join gate, cancel, extend,
        pause, end, the two Celery sweeps…). A new terminal status would have
        to be added to every one of them, and missing a single guard would let
        someone join a dead session — a far worse bug than the reporting gap
        this fixes. The information is already in the data: the class reached
        its end and `actual_started_at` was never stamped, meaning nobody ever
        joined the room.

        Cancelled is excluded: someone called that class off on purpose, which
        is a different fact from a teacher who simply never appeared.
        """
        if self.status == self.STATUS_CANCELLED:
            return False
        return (self.actual_started_at is None
                and self.computed_status() == self.STATUS_COMPLETED)

    def display_status(self):
        """What a human should be shown. Reports and rosters read this;
        lifecycle code must keep reading computed_status()."""
        if self.was_missed:
            return "MISSED"
        return self.computed_status()

    @property
    def hard_end_time(self):
        """The moment this session is genuinely over.

        Every terminal check should use this, never end_time — end_time is
        the PLANNED end and is a scheduling value, not a lifecycle one.
        """
        return (self.extended_until or self.end_time) + self.LIVE_GRACE

    @property
    def max_extension_time(self):
        return self.end_time + self.MAX_EXTENSION

    # 🔥 CORE STATE LOGIC (VERY IMPORTANT)
    def computed_status(self):
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()

        if self.status == self.STATUS_CANCELLED:
            return self.STATUS_CANCELLED

        # Already explicitly completed
        if self.status == self.STATUS_COMPLETED:
            return self.STATUS_COMPLETED

        # Past the planned end AND past the overrun grace → completed.
        # Deliberately hard_end_time, not end_time: see LIVE_GRACE.
        if now >= self.hard_end_time:
            return self.STATUS_COMPLETED

        # Manual pause takes priority over teacher_left_at timer
        if self.status == self.STATUS_PAUSED and not self.teacher_left_at:
            return self.STATUS_PAUSED

        if self.teacher_left_at:
            diff = now - self.teacher_left_at

            # 0–10 min → reconnecting
            if diff <= timedelta(minutes=10):
                return self.STATUS_RECONNECTING

            # 10–60 min → paused
            if diff <= timedelta(minutes=60):
                return self.STATUS_PAUSED

            # >60 min → completed
            return self.STATUS_COMPLETED

        if self.status == self.STATUS_LIVE and not self.teacher_left_at:
            return self.STATUS_LIVE

        if now < self.start_time:
            return self.STATUS_SCHEDULED

        return self.STATUS_WAITING

    def sync_status(self, *, save=True):
        """Persist the derived status to the stored column.

        computed_status() stays the READ path (pure, derived); this is the
        WRITE path that keeps the `status` column honest, called by the
        1-minute Celery sweep (livestream.tasks.sync_open_session_statuses)
        so the reconnection ladder (RECONNECTING → PAUSED → COMPLETED)
        advances on a timer instead of only when someone reads the session,
        and raw `status=` queries agree with what users see.

        Returns (changed: bool, new_status: str). When `changed` is True the
        caller should broadcast the update. Terminal states (COMPLETED /
        CANCELLED) are never moved off of.
        """
        # Never resurrect a terminal session.
        if self.status in (self.STATUS_COMPLETED, self.STATUS_CANCELLED):
            return (False, self.status)

        new_status = self.computed_status()
        if new_status == self.status:
            return (False, self.status)

        self.status = new_status
        # A session that has reached COMPLETED via the ladder should also drop
        # the reconnect timer so it can't be re-read as RECONNECTING/PAUSED.
        if new_status == self.STATUS_COMPLETED:
            self.teacher_left_at = None
            if save:
                self.save(update_fields=["status", "teacher_left_at"])
        elif save:
            self.save(update_fields=["status"])
        return (True, new_status)



class LiveSessionSpectate(models.Model):
    """An admin watched a live class.

    Spectating is silent by design — the grant sets hidden=True, so neither
    the teacher nor the students are told. That is a deliberate product
    decision, and it is exactly why this table exists: monitoring that leaves
    no trace anywhere is not a capability this codebase should offer. The room
    stays unaware; the action does not go unrecorded.

    Kept even when the admin or session is deleted is NOT attempted — the FK
    cascades — but `admin_email` is denormalised so the trail survives the
    account being renamed or the profile changing.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="spectate_events",
    )
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="live_spectate_events",
    )
    admin_email = models.CharField(max_length=254, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
            models.Index(fields=["admin", "created_at"]),
        ]

    def __str__(self):
        return f"{self.admin_email} spectated {self.session_id}"


class LiveSessionRemoval(models.Model):
    """A participant the teacher removed from a live class.

    This table is the ONLY thing that makes an ejection stick. A LiveKit
    token is a bearer credential — there is no server-side blocklist and no
    revocation API — and the livestream token TTL is 2 hours. So calling
    LiveKit's RemoveParticipant merely disconnects someone; they can rejoin
    seconds later using the token already in their browser, and before this
    existed there was nothing anywhere in the codebase to stop them (the only
    LiveKit admin call was close_room, which is all-or-nothing and ends the
    class for everybody).

    The join endpoint checks this table before minting a token, so removal
    survives a rejoin, a refresh and a new tab. Scoped to one session: being
    removed from Tuesday's class must not bar the student from Thursday's.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="removals",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="live_session_removals",
    )
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="live_session_removals_made",
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set when a teacher readmits someone removed by mistake. Kept as a row
    # rather than deleted so the moderation history stays auditable.
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["session", "user", "revoked_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="uniq_active_removal_per_session_user",
            )
        ]

    def __str__(self):
        return f"{self.user_id} removed from {self.session_id}"


class LiveSessionChatMessage(models.Model):
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    sender_name = models.CharField(max_length=200)
    text = models.TextField()
    is_teacher = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["session", "created_at"])]

    def __str__(self):
        return f"{self.sender_name}: {self.text[:50]}"


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

    # ── ROLLUP semantics ──
    # joined_at = FIRST join, left_at = LAST leave, total_seconds = summed watch
    # time across every join/leave cycle (see LiveSessionAttendanceInterval for
    # the append-only per-cycle rows). unique_together keeps exactly one rollup
    # row per (session, user); rejoins no longer overwrite prior attendance —
    # they extend total_seconds instead.
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    total_seconds = models.PositiveIntegerField(default=0)

    # WHICH child. One email is one account holding many LearnerProfiles, and
    # keying attendance on the account alone merged two siblings' watch time
    # into one row that then appeared on both their records. NULL for
    # teachers (who have no learner profile) and for rows written before this
    # existed.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="live_attendances",
    )

    class Meta:
        # (session, user) was the old key, which is exactly what merged the
        # siblings. NOTE the NULL caveat: SQL treats NULLs as distinct, so
        # this does not constrain teacher rows — harmless, because the ORM
        # path upserts on learner_profile=None (an IS NULL match) and so still
        # reuses the one row.
        unique_together = ("session", "user", "learner_profile")
        indexes = [
            models.Index(fields=["session", "user"]),
            models.Index(fields=["session", "learner_profile"]),
            models.Index(fields=["session", "joined_at"]),
        ]

    # ✅ duration tracking (important for analytics)
    def duration(self):
        if self.total_seconds:
            from datetime import timedelta
            return timedelta(seconds=self.total_seconds)
        if self.joined_at and self.left_at:
            return self.left_at - self.joined_at
        return None


class SessionReview(models.Model):
    """A participant's post-class rating for one LiveSession.

    One review per (session, user) — resubmitting overwrites via
    update_or_create rather than erroring, so the "review on leave" prompt
    can be re-shown/re-submitted without a unique-constraint 500.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="reviews",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    rating = models.PositiveSmallIntegerField(
        choices=[(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "user")
        indexes = [models.Index(fields=["session", "user"])]

    def __str__(self):
        return f"{self.user_id} rated {self.session_id}: {self.rating}"


class SessionNote(models.Model):
    """A participant's private notes for one LiveSession.

    Private per (session, user) — a user only ever sees their own note for a
    session, never another participant's. Upserted via update_or_create so the
    in-call Notes panel can autosave without worrying about create-vs-update.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    content = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("session", "user")
        indexes = [models.Index(fields=["session", "user"])]

    def __str__(self):
        return f"{self.user_id} notes on {self.session_id}"


class LiveSessionAttendanceInterval(models.Model):
    """Append-only, one row per join→leave cycle.

    The authoritative attendance record. `LiveSessionAttendance` is a derived
    rollup over these. A user who disconnects and rejoins produces multiple
    rows, so reconnect history, total watch time, and true presence intervals
    are all recoverable — which the single-interval rollup alone can't do.
    `closed_by_reconcile` marks rows we closed defensively (missed
    participant_left webhook, or room_finished sweep) rather than a real leave.
    """
    session = models.ForeignKey(
        LiveSession,
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

    # Which child was actually in the room — see LiveSessionAttendance.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="live_attendance_intervals",
    )

    class Meta:
        ordering = ["joined_at"]
        indexes = [
            models.Index(fields=["session", "user"]),
            models.Index(fields=["session", "learner_profile"]),
            models.Index(fields=["session", "joined_at"]),
            models.Index(fields=["session", "left_at"]),
        ]

    def duration_seconds(self):
        if self.joined_at and self.left_at:
            return int((self.left_at - self.joined_at).total_seconds())
        return 0


class LiveKitWebhookEvent(models.Model):
    """Durable, idempotent log of every LiveKit webhook we receive.

    Written BEFORE dispatch so nothing is lost if a handler throws; `event_id`
    is unique so redelivered events are skipped. This is the audit trail that
    lets us reconcile attendance/state if a handler ever fails mid-flight.
    """
    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64)
    room_name = models.CharField(max_length=255, blank=True)
    session = models.ForeignKey(
        LiveSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="webhook_events",
    )
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["event_type"]),
            models.Index(fields=["room_name"]),
            models.Index(fields=["received_at"]),
            models.Index(fields=["processed"]),
        ]

    def __str__(self):
        return f"{self.event_type} · {self.room_name} · {self.received_at:%Y-%m-%d %H:%M}"


class LiveSessionViewerSample(models.Model):
    """Periodic concurrent-viewer snapshot for a live session.

    Written by the reconciliation/sampling Celery task (LiveKit participant
    poll) so we retain a time-series of watching counts and can finalize
    LiveSession.peak_viewers. Ephemeral WS group membership can't give us this.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="viewer_samples",
    )
    ts = models.DateTimeField(auto_now_add=True)
    viewers = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["ts"]
        indexes = [models.Index(fields=["session", "ts"])]


class StreamHealthSample(models.Model):
    """Client-reported stream health telemetry.

    LiveKit does not push quality stats over webhooks, so the presenter/viewer
    clients POST periodic samples to /livestream/sessions/<id>/health/. This is
    the durable capture path for bitrate / fps / latency / packet-loss the admin
    Livestream Monitor renders.
    """
    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="health_samples",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    ts = models.DateTimeField(auto_now_add=True)
    bitrate_kbps = models.PositiveIntegerField(null=True, blank=True)
    fps = models.PositiveIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    packet_loss = models.FloatField(null=True, blank=True)  # fraction 0..1
    quality = models.CharField(max_length=20, blank=True)  # excellent|good|poor
    is_presenter = models.BooleanField(default=False)

    class Meta:
        ordering = ["-ts"]
        indexes = [
            models.Index(fields=["session", "ts"]),
            models.Index(fields=["session", "is_presenter", "ts"]),
        ]


class LiveSessionEgress(models.Model):
    """One ATTEMPT at auto-recording a live class through LiveKit Egress.

    A row per attempt rather than a field on LiveSession, because a single
    class legitimately produces several: the teacher's connection drops and
    they rejoin, or LiveKit's egress worker dies mid-class and a replacement
    is started. Flattening that onto LiveSession would silently discard every
    attempt but the last, which is precisely the case where you need the
    history to work out what happened to a missing recording.

    Lifecycle, and how to read it — `status` tracks ONLY LiveKit's own egress
    state machine. What happens afterwards (hand the file to Bunny Stream,
    wait for transcode, delete the raw mp4) is deliberately NOT a second set
    of status values, because that state is already derivable and two
    overlapping state machines drift:

        status=REQUESTED                    → the start call hasn't returned
        status=EGRESS_COMPLETE, recording   → ended, not yet handed to Stream
                            is NULL
        recording set, recording.status < 4 → Bunny Stream is transcoding
        raw_deleted_at set                  → done; the raw mp4 is purged

    `storage_key` is the object key inside BUNNY_EGRESS_ZONE. It carries a
    random segment on purpose: between egress finishing and the Stream fetch
    completing, that object is readable by anyone who can guess its URL (see
    config/settings_base.py's BUNNY_EGRESS_* block for why it must be
    briefly public at all).
    """

    # Local state: the start call has been issued but LiveKit hasn't returned
    # an egress id yet, so there is nothing to correlate a webhook against.
    STATUS_REQUESTED = "REQUESTED"
    # Local state: the start call itself raised. Distinct from EGRESS_FAILED,
    # which is LiveKit telling us a real egress died — this one never began,
    # and `error` holds the exception.
    STATUS_START_FAILED = "START_FAILED"

    # Mirrors livekit.protocol EgressStatus, stored as its string name so the
    # value stays readable in the admin and survives an SDK enum renumbering.
    STATUS_STARTING = "EGRESS_STARTING"
    STATUS_ACTIVE = "EGRESS_ACTIVE"
    STATUS_ENDING = "EGRESS_ENDING"
    STATUS_COMPLETE = "EGRESS_COMPLETE"
    STATUS_FAILED = "EGRESS_FAILED"
    STATUS_ABORTED = "EGRESS_ABORTED"
    STATUS_LIMIT_REACHED = "EGRESS_LIMIT_REACHED"

    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_START_FAILED, "Start failed"),
        (STATUS_STARTING, "Starting"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_ENDING, "Ending"),
        (STATUS_COMPLETE, "Complete"),
        (STATUS_FAILED, "Failed"),
        (STATUS_ABORTED, "Aborted"),
        (STATUS_LIMIT_REACHED, "Limit reached"),
    ]

    # Terminal states in which no further LiveKit webhook will ever arrive.
    TERMINAL_STATUSES = (
        STATUS_START_FAILED,
        STATUS_COMPLETE,
        STATUS_FAILED,
        STATUS_ABORTED,
        STATUS_LIMIT_REACHED,
    )

    session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="egresses",
    )

    # LiveKit's egress id. Blank only in REQUESTED/START_FAILED — every
    # webhook correlates on this, so it is indexed and unique-when-present
    # (a partial constraint, since several in-flight rows may hold "").
    egress_id = models.CharField(max_length=64, blank=True, db_index=True)

    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
    )

    # Object key inside BUNNY_EGRESS_ZONE, e.g.
    # "class-egress/<session_id>/<random>.mp4".
    storage_key = models.CharField(max_length=512, blank=True)

    # The Bunny Stream row this attempt eventually became. NULL until the
    # fetch hop runs; SET_NULL so deleting a recording leaves the audit trail
    # of how it got here intact.
    recording = models.ForeignKey(
        "courses.SessionRecording",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="egress_attempts",
    )

    # Last failure seen, from either the start call or an egress_ended event
    # carrying an error. Kept rather than only logged: by the time anyone
    # asks why a class has no recording, the log line has rotated away.
    error = models.TextField(blank=True)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Reported by LiveKit on egress_ended; used to spot a truncated or
    # zero-byte capture before it is handed to Bunny Stream.
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    # Number of times the Bunny Stream fetch hop has been attempted, so a
    # permanently unfetchable file stops being retried forever.
    fetch_attempts = models.PositiveSmallIntegerField(default=0)

    # When the raw mp4 was purged from Bunny Storage. Also the "this attempt
    # is fully done" marker — see the lifecycle note above. NULL while the
    # object is still sitting on a public pull zone.
    raw_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        verbose_name_plural = "live session egresses"
        indexes = [
            models.Index(fields=["session", "-requested_at"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["egress_id"],
                condition=~models.Q(egress_id=""),
                name="uniq_livesessionegress_egress_id_when_set",
            ),
        ]

    def __str__(self):
        return f"{self.session_id} · {self.egress_id or 'pending'} · {self.status}"

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def awaiting_stream_fetch(self):
        """Ended cleanly, has a file, and nothing has pulled it into Bunny
        Stream yet — the queue the phase-3 fetch task drains."""
        return (
            self.status == self.STATUS_COMPLETE
            and bool(self.storage_key)
            and self.recording_id is None
        )
