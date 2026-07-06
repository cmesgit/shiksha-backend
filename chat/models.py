# PLACEMENT: backend/backend/chat/models.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/models.py
"""
chat/models.py

Real-time chat for ShikshaCom dashboards.

IDENTITY MODEL (matches the multi-profile account system):
  A *participant* is NOT a User. It is a specific identity on an account:
    - a LearnerProfile  (a learner — the holder or a dependent child), or
    - a TeacherProfile  (the teacher identity — faculty AND/OR guest expert).
  This is "per active profile": child A and child B on the same account chat
  as separate participants, and the teacher identity is its own inbox.

  A "guest expert" and a "faculty teacher" are the SAME TeacherProfile seen
  through its two approved tracks (skill_status / academy_status); both are
  KIND_TEACHER here. A "skill-dev student" and an "academy student" are the
  SAME LearnerProfile seen through the two tracks; both are KIND_LEARNER.
  Roles are computed for display/filtering in services.participant_roles().

CONVERSATION KINDS:
  DIRECT    — 1:1 between exactly two participants (e.g. learner <-> teacher).
  ROOM      — a group room scoped to some owning context, identified by
              (context_type, context_id) — a soft reference, same pattern as
              accounts.Identity.profile_id (see the field comment below).
              The only owner today is "course" (participants are everyone
              enrolled, as their learner profile, plus the course's
              teacher(s)); "counseling_case" exists as a valid context_type
              from M3 onward but nothing creates one until M4 wires the
              Counselling vertical's booking flow into chat.
              (M3 — Phase 3 §9. Formerly a COURSE kind with a single
              `course_id` UUIDField; old COURSE rows migrate to
              kind=ROOM/context_type="course" — see migration 0006. The
              read-only `course_id` property below exists purely so code
              that hasn't migrated to context_id yet keeps working.)
  SESSION, SUPPORT, BROADCAST — reserved kinds widened in from M3 (Phase 3
              §9); no chat code creates these yet. BROADCAST is read-only at
              the policy layer (chat/policy.py's can_post()) the moment a
              conversation has this kind, regardless of who's asking.

BLOCKING (added):
  A Block is one identity silencing another. Permission is enforced in the
  views (chat/views.py), per the platform rule:
    • a TEACHER (faculty or guest expert) can block ANY user;
    • a LEARNER (academy or skill-dev student) can block other LEARNERS only —
      never a teacher / guest expert.
  Enforcement at send time lives in services.post_message_checked(): if a block
  exists in EITHER direction between the two parties of a direct thread, the
  message is refused.

Identity is stored polymorphically via (kind, learner_profile, teacher_profile)
rather than a generic FK, to keep queries simple and indexed.
"""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q

from accounts.models import LearnerProfile, TeacherProfile, Identity


class Conversation(models.Model):
    KIND_DIRECT = "DIRECT"
    KIND_ROOM = "ROOM"
    KIND_SESSION = "SESSION"
    KIND_SUPPORT = "SUPPORT"
    KIND_BROADCAST = "BROADCAST"
    KIND_CHOICES = [
        (KIND_DIRECT, "Direct (1:1)"),
        (KIND_ROOM, "Group room"),
        (KIND_SESSION, "Session-scoped chat"),
        (KIND_SUPPORT, "Support thread"),
        (KIND_BROADCAST, "Broadcast (read-only)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)

    # M3 (Phase 3 §9): a ROOM's owner, as a soft reference — the same
    # pattern accounts.Identity.profile_id already uses, for the same
    # reason. context_id is a CharField, NOT a UUIDField: different context
    # owners have different PK types (Course=UUID; counseling's Appointment
    # is a plain integer AutoField — confirmed by reading
    # counseling/models.py, not assumed). A UUIDField would silently coerce
    # an integer id into a fake UUID instead of erroring — the same
    # UUID-vs-int lesson M1's Identity.profile_id already learned. Blank for
    # DIRECT and for the reserved SESSION/SUPPORT/BROADCAST kinds, which
    # don't use this generalization (yet).
    context_type = models.CharField(max_length=30, blank=True, default="")
    context_id = models.CharField(max_length=64, null=True, blank=True)
    title = models.CharField(max_length=200, blank=True)

    # M3 (Phase 3 §10): structural read-only gate consulted by
    # policy.can_post() before moderation. Nothing in this stage ever sets
    # this True — the field and the gate are the whole of M3's work here,
    # the same "possible, not wired" shape as context_type="counseling_case"
    # above: a later stage (course completion? explicit archive action?)
    # decides when a room actually freezes.
    is_frozen = models.BooleanField(default=False)

    # For DIRECT rooms: a stable de-dupe key so we never create two 1:1 rooms
    # for the same pair. Built from the sorted participant identity keys.
    direct_key = models.CharField(max_length=120, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["direct_key"],
                condition=Q(kind="DIRECT"),
                name="unique_direct_conversation",
            ),
            # M3: replaces unique_course_conversation (was: unique course_id
            # where kind=COURSE). A ROOM is unique per (context_type,
            # context_id) rather than per course_id alone, since ROOM can
            # now be owned by more than one vertical.
            models.UniqueConstraint(
                fields=["context_type", "context_id"],
                condition=Q(kind="ROOM"),
                name="unique_room_per_context",
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "context_type", "context_id"]),
        ]

    def group_name(self):
        """Channels group name for this conversation."""
        return f"chat_{self.id}"

    @property
    def course_id(self):
        """Read-only back-compat shim for the pre-M3 `course_id` UUIDField
        (Phase 3 §9). Returns the course's id, as a string, for a course
        ROOM — else None. Every existing caller already treats this as an
        opaque id to str()/filter-by (chat/services.py's
        course_room_track(), serialize_conversation()), so the type change
        from UUID object to str is not a behaviour change for them."""
        if self.context_type == "course":
            return self.context_id
        return None

    def __str__(self):
        return f"{self.kind} · {self.title or self.id}"


class Participant(models.Model):
    KIND_LEARNER = "LEARNER"
    KIND_TEACHER = "TEACHER"
    KIND_CHOICES = [
        (KIND_LEARNER, "Learner profile"),
        (KIND_TEACHER, "Teacher identity"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="participants"
    )

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    learner_profile = models.ForeignKey(
        LearnerProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_participations",
    )
    teacher_profile = models.ForeignKey(
        TeacherProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_participations",
    )

    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    # M1 (Phase 3 §6): dual-written alongside the polymorphic columns above
    # by services._attach_participant(). NOTE: this field, and Block's
    # blocker_identity/blocked_identity below, were already added to the DB
    # by migration 0004 and backfilled by 0005 — this models.py file was
    # simply missing the corresponding field declarations (a drift between
    # the model source and the already-applied migration history, found
    # while proving the M3 migration path against a real DB per this
    # stage's brief; every value/related_name here matches 0004 exactly,
    # so this is a restoration, not a new field). Kept nullable/SET_NULL
    # exactly as M1 shipped it — no behaviour changes as part of M3.
    identity = models.ForeignKey(
        Identity, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="chat_participations_v2",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "learner_profile"],
                condition=Q(kind="LEARNER"),
                name="unique_learner_per_conversation",
            ),
            models.UniqueConstraint(
                fields=["conversation", "teacher_profile"],
                condition=Q(kind="TEACHER"),
                name="unique_teacher_per_conversation",
            ),
        ]
        indexes = [
            models.Index(fields=["learner_profile"]),
            models.Index(fields=["teacher_profile"]),
        ]

    # --- identity helpers ---
    def identity_key(self):
        """Stable string identifying this participant across the system."""
        if self.kind == self.KIND_LEARNER:
            return f"L:{self.learner_profile_id}"
        return f"T:{self.teacher_profile_id}"

    @property
    def account_id(self):
        if self.kind == self.KIND_LEARNER and self.learner_profile_id:
            return self.learner_profile.account_id
        if self.kind == self.KIND_TEACHER and self.teacher_profile_id:
            return self.teacher_profile.user_id
        return None

    def account(self):
        """Same resolution as account_id, but returns the User instance
        itself. M3: outbox_handlers needs an actual recipient object for
        notifications.services.notify(recipient=...), not just its id."""
        if self.kind == self.KIND_LEARNER and self.learner_profile_id:
            return self.learner_profile.account
        if self.kind == self.KIND_TEACHER and self.teacher_profile_id:
            return self.teacher_profile.user
        return None

    def display_name(self):
        if self.kind == self.KIND_LEARNER and self.learner_profile:
            return self.learner_profile.display_name
        if self.kind == self.KIND_TEACHER and self.teacher_profile:
            return self.teacher_profile.user.username or "Teacher"
        return "Unknown"

    def avatar(self):
        if self.kind == self.KIND_LEARNER and self.learner_profile:
            return self.learner_profile.avatar_value()
        if self.kind == self.KIND_TEACHER and self.teacher_profile and self.teacher_profile.photo:
            return self.teacher_profile.photo.url
        return None

    def __str__(self):
        return f"{self.display_name()} in {self.conversation_id}"


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        Participant, on_delete=models.SET_NULL, null=True, related_name="sent_messages"
    )
    body = models.TextField()

    # Idempotency for at-least-once websocket delivery / optimistic UI.
    client_id = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["conversation", "client_id"]),
        ]
        constraints = [
            # M0 hardening: a WS reconnect can replay the same optimistic send.
            # post_message() already checks client_id before inserting, but
            # that check-then-insert has a race under concurrent retries; this
            # constraint is the actual guarantee. Scoped to non-blank client_id
            # (server-authored / legacy rows with no client_id are unaffected)
            # and per-sender (two different senders coincidentally generating
            # the same client-side id must not collide).
            models.UniqueConstraint(
                fields=["conversation", "sender", "client_id"],
                condition=~Q(client_id=""),
                name="unique_message_client_id_per_sender",
            ),
        ]

    def __str__(self):
        return f"msg {self.id} in {self.conversation_id}"


# ===========================================================================
# BLOCKING
# ===========================================================================

class Block(models.Model):
    """One identity silencing another.

    `pair_key` is the de-dupe key "<blockerIdentity>><blockedIdentity>", e.g.
    "T:<uuid>>L:<uuid>". It is set by services.create_block(); the unique
    constraint on it stops duplicate block rows.
    """
    KIND_LEARNER = "LEARNER"
    KIND_TEACHER = "TEACHER"
    KIND_CHOICES = [
        (KIND_LEARNER, "Learner profile"),
        (KIND_TEACHER, "Teacher identity"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # who is doing the blocking
    blocker_kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    blocker_learner = models.ForeignKey(
        LearnerProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_blocks_made",
    )
    blocker_teacher = models.ForeignKey(
        TeacherProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_blocks_made",
    )

    # who is being blocked
    blocked_kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    blocked_learner = models.ForeignKey(
        LearnerProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_blocks_received",
    )
    blocked_teacher = models.ForeignKey(
        TeacherProfile, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_blocks_received",
    )

    pair_key = models.CharField(max_length=120, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # M1 — see the identical note on Participant.identity above. Restored
    # verbatim from migration 0004; dual-written by services.create_block().
    blocker_identity = models.ForeignKey(
        Identity, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="identity_blocks_made",
    )
    blocked_identity = models.ForeignKey(
        Identity, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="identity_blocks_received",
    )

    class Meta:
        indexes = [
            models.Index(fields=["blocker_learner"]),
            models.Index(fields=["blocker_teacher"]),
            models.Index(fields=["blocked_learner"]),
            models.Index(fields=["blocked_teacher"]),
        ]

    def __str__(self):
        return f"Block {self.pair_key}"


# ===========================================================================
# TRANSACTIONAL OUTBOX  (M3 — Phase 3 §11)
# ===========================================================================

class OutboxEvent(models.Model):
    """Written in the SAME transaction as the Message that caused it (see
    services.post_message()) — that's the whole guarantee: if the message
    commit rolls back, so does this row; if the message commit succeeds,
    this row is guaranteed to exist for the relay to find even if the
    process dies the instant after commit. This is what finally gives
    OFFLINE users a "new message" notification (gap G5) — the M0
    inbox_delta (chat/realtime.py) only reaches an already-open socket.

    chat/outbox_handlers.py drains this table (via a ~10s Celery-beat
    task — config/celery.py uses a float schedule here, since crontab's
    floor is 1 minute), turning each row into one notifications.services
    .notify() call per recipient, carrying M2's audience_identity.

    Delivery is AT-LEAST-ONCE, not exactly-once: `attempts` bounds retries
    so a permanently-broken row can't retry forever, but a transient
    failure is retried on the next drain rather than silently dropped. A
    duplicate notification on a retried row is an acceptable cost; a
    silently lost one is not.
    """
    EVENT_MESSAGE_CREATED = "chat.message_created"
    EVENT_CHOICES = [
        (EVENT_MESSAGE_CREATED, "Message created"),
    ]

    MAX_ATTEMPTS = 5

    event_type = models.CharField(max_length=50, choices=EVENT_CHOICES)
    # e.g. {"conversation_id": "<uuid>", "message_id": "<uuid>"}. Carries
    # ids only, never denormalized state — outbox_handlers always refetches
    # the live Message/Conversation, so a row is never stale even if it
    # sits in the queue a while.
    payload = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            # The relay's hot-path query: unprocessed rows, oldest first.
            models.Index(fields=["processed_at", "created_at"]),
        ]

    def __str__(self):
        return f"OutboxEvent<{self.event_type}> #{self.pk}"
