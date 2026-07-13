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
from django.utils import timezone

from accounts.models import LearnerProfile, TeacherProfile, Identity
from .attachments import validate_chat_attachment


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
            # Stage D (Announcements, CC-015): one BROADCAST channel per
            # context, same idea as the ROOM constraint above and kept as a
            # SEPARATE constraint (rather than widening unique_room_per_context
            # to cover both kinds) so a course can have both its ROOM
            # (Discussion) and its BROADCAST (Announcements) — two rows
            # sharing one (context_type, context_id) pair, distinguished by
            # kind — without the two constraints fighting each other.
            models.UniqueConstraint(
                fields=["context_type", "context_id"],
                condition=Q(kind="BROADCAST"),
                name="unique_broadcast_per_context",
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
    # M4 (Communication Center closure — Stage D): a non-profile identity for
    # admin/support-desk staff replying inside a SUPPORT ticket's conversation
    # (CC-022) or authoring a course BROADCAST on the platform's behalf. Not
    # a LearnerProfile or TeacherProfile — backed directly by the auth User
    # (`staff_user` below), gated at the view layer on `request.user.is_staff`
    # (accounts.permissions.IsAdmin), never self-service like the other two
    # kinds. Chosen over repurposing KIND_TEACHER so a staff reply is never
    # mistaken for the learner's own faculty/guest-expert relationships, and
    # over a parallel messaging system so support tickets reuse the exact
    # same moderation/serialization/realtime pipeline every other message
    # already goes through.
    KIND_STAFF = "STAFF"
    KIND_CHOICES = [
        (KIND_LEARNER, "Learner profile"),
        (KIND_TEACHER, "Teacher identity"),
        (KIND_STAFF, "Support / admin staff"),
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
    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_staff_participations",
    )

    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    # CC-006 conversation-card actions (Communication Center closure — Stage
    # B). Per-Participant (not per-Conversation), so pinning/archiving/muting
    # a thread is scoped to the identity that did it — profile isolation is
    # preserved for free, exactly like last_read_at already is. `archived_at`
    # doubles as the "Delete" action from CC-006: the spec lists a literal
    # "Delete", but with profile-isolated participants a destructive delete
    # would either orphan the other side's copy of the thread or cascade
    # across identities that never asked for that — see the gap analysis's
    # own flag on this ("should be per-identity hide/archive, not destructive
    # delete — the spec doesn't say, the implementation should"). So "Delete"
    # in the UI calls the same archive endpoint; nothing is ever destroyed.
    pinned = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    muted_until = models.DateTimeField(
        null=True, blank=True,
        help_text="Notifications for this thread are suppressed until this "
                   "time. Null = not muted.",
    )

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
            models.UniqueConstraint(
                fields=["conversation", "staff_user"],
                condition=Q(kind="STAFF"),
                name="unique_staff_per_conversation",
            ),
        ]
        indexes = [
            models.Index(fields=["learner_profile"]),
            models.Index(fields=["teacher_profile"]),
            models.Index(fields=["staff_user"]),
        ]

    # --- identity helpers ---
    def identity_key(self):
        """Stable string identifying this participant across the system.
        "S:<user_id>" for staff deliberately reuses the single-letter "S"
        accounts.Identity.KIND_SYSTEM already reserves for exactly this
        ("announcement / support bot senders")."""
        if self.kind == self.KIND_LEARNER:
            return f"L:{self.learner_profile_id}"
        if self.kind == self.KIND_TEACHER:
            return f"T:{self.teacher_profile_id}"
        return f"S:{self.staff_user_id}"

    @property
    def account_id(self):
        if self.kind == self.KIND_LEARNER and self.learner_profile_id:
            return self.learner_profile.account_id
        if self.kind == self.KIND_TEACHER and self.teacher_profile_id:
            return self.teacher_profile.user_id
        if self.kind == self.KIND_STAFF and self.staff_user_id:
            return self.staff_user_id
        return None

    def account(self):
        """Same resolution as account_id, but returns the User instance
        itself. M3: outbox_handlers needs an actual recipient object for
        notifications.services.notify(recipient=...), not just its id."""
        if self.kind == self.KIND_LEARNER and self.learner_profile_id:
            return self.learner_profile.account
        if self.kind == self.KIND_TEACHER and self.teacher_profile_id:
            return self.teacher_profile.user
        if self.kind == self.KIND_STAFF and self.staff_user_id:
            return self.staff_user
        return None

    def display_name(self):
        if self.kind == self.KIND_LEARNER and self.learner_profile:
            return self.learner_profile.display_name
        if self.kind == self.KIND_TEACHER and self.teacher_profile:
            return self.teacher_profile.user.username or "Teacher"
        if self.kind == self.KIND_STAFF and self.staff_user:
            return self.staff_user.get_full_name() or self.staff_user.username or "Support Team"
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
    TYPE_TEXT = "TEXT"
    TYPE_IMAGE = "IMAGE"
    TYPE_FILE = "FILE"
    TYPE_SYSTEM = "SYSTEM"
    TYPE_ANNOUNCEMENT = "ANNOUNCEMENT"
    TYPE_CHOICES = [
        (TYPE_TEXT, "Text"),
        (TYPE_IMAGE, "Image attachment"),
        (TYPE_FILE, "File attachment"),
        (TYPE_SYSTEM, "System message"),
        (TYPE_ANNOUNCEMENT, "Announcement"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        Participant, on_delete=models.SET_NULL, null=True, related_name="sent_messages"
    )
    body = models.TextField()

    # CC-010 (Communication Center closure — Stage C): TEXT is everything
    # that shipped before this stage. IMAGE/FILE are set the moment a
    # MessageAttachment is created alongside this row (see
    # services.post_attachment_checked()) — `body` is then that message's
    # optional caption, may be blank. SYSTEM is reserved for future
    # server-authored rows (e.g. "X joined the class"); nothing writes it
    # yet. ANNOUNCEMENT is set for messages posted into a KIND_BROADCAST
    # conversation (see services.post_message()) purely so a client can
    # style/label them without having to also fetch the parent conversation.
    message_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default=TYPE_TEXT)

    # CC-007/010 reply-to-message (Stage B). SET_NULL (not CASCADE): deleting
    # the parent message must not cascade-delete every reply to it — the
    # reply just loses its quoted context (serialize_message() renders
    # "Original message deleted" when reply_to_id survives but reply_to is
    # gone... actually SET_NULL means reply_to_id itself becomes NULL, so
    # the reply degrades to looking like a plain message; see
    # services.serialize_message()).
    reply_to = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="replies",
    )

    # Idempotency for at-least-once websocket delivery / optimistic UI.
    client_id = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # Soft delete (Stage B). Never hard-deleted: a moderation trail and
    # "message deleted" placeholders both need the row to still exist.
    # `deleted_by` is set for a participant-initiated delete (the sender
    # removing their own message, or — inside a ROOM only — a TEACHER
    # participant exercising in-room moderation; see
    # services.can_delete_message()). `deleted_reason` is populated ONLY for
    # an admin/moderator removal (services.soft_delete_message() called with
    # admin_reason=...) — its presence is what lets the frontend show
    # "Removed by a moderator" instead of the plain "Message deleted" a
    # self-delete gets, WITHOUT needing a Participant row for the acting
    # admin (an admin is not, and should not become, a participant of an
    # arbitrary DIRECT/ROOM conversation just to remove one message from it —
    # see chat/views.py's AdminRemoveMessageView).
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        Participant, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="deleted_messages",
    )
    deleted_reason = models.CharField(max_length=255, blank=True, default="")

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


# ===========================================================================
# ATTACHMENTS  (Communication Center closure — Stage C · CC-010/012/016)
# ===========================================================================

def chat_attachment_upload_path(instance, filename):
    """chat_attachments/<conversation_id>/<uuid>_<original filename> — the
    uuid prefix avoids same-name collisions inside one conversation without
    needing a directory-per-message."""
    return f"chat_attachments/{instance.conversation_id}/{uuid.uuid4().hex}_{filename}"


class MessageAttachment(models.Model):
    """One file per message (OneToOne) — sending three photos is three
    messages, each carrying one attachment. This is a deliberate
    simplification versus a many-attachments-per-message model: it keeps
    the composer, the bubble renderer, and the realtime payload shape all
    single-item, and matches how most chat products actually render a
    multi-image send (as a stack of individual bubbles) rather than a
    single multi-file bubble.
    """
    KIND_IMAGE = "IMAGE"
    KIND_PDF = "PDF"
    KIND_DOCUMENT = "DOCUMENT"
    KIND_OTHER = "OTHER"
    KIND_CHOICES = [
        (KIND_IMAGE, "Image"),
        (KIND_PDF, "PDF"),
        (KIND_DOCUMENT, "Document"),
        (KIND_OTHER, "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="attachments"
    )
    message = models.OneToOneField(
        Message, on_delete=models.CASCADE, related_name="attachment"
    )
    uploaded_by = models.ForeignKey(
        Participant, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="uploaded_attachments",
    )
    file = models.FileField(
        upload_to=chat_attachment_upload_path,
        validators=[validate_chat_attachment],
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_OTHER)
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def filename(self):
        if self.original_name:
            return self.original_name
        if not self.file:
            return "file"
        return self.file.name.rsplit("/", 1)[-1]

    def __str__(self):
        return f"{self.kind} attachment on msg {self.message_id}"


# ===========================================================================
# REACTIONS  (Stage B · CC-007/010/015)
# ===========================================================================

class MessageReaction(models.Model):
    """One (message, participant, emoji) row per reaction. A participant can
    react to the same message with several different emoji, but not the same
    emoji twice — sending the same emoji again is a client-side TOGGLE
    (services.toggle_reaction()) that removes this row instead of erroring.
    The allowed emoji set itself is enforced in services.py
    (ALLOWED_REACTIONS), not here, so widening it later never needs a
    migration."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="reactions_made"
    )
    emoji = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "participant", "emoji"],
                name="unique_reaction_per_participant_emoji",
            ),
        ]
        indexes = [models.Index(fields=["message"])]

    def __str__(self):
        return f"{self.emoji} on msg {self.message_id}"


# ===========================================================================
# REPORTS  (Stage B/D · CC-006/010/023 — the admin moderation queue)
# ===========================================================================

class Report(models.Model):
    REASON_SPAM = "SPAM"
    REASON_HARASSMENT = "HARASSMENT"
    REASON_INAPPROPRIATE = "INAPPROPRIATE"
    REASON_OTHER = "OTHER"
    REASON_CHOICES = [
        (REASON_SPAM, "Spam or scam"),
        (REASON_HARASSMENT, "Harassment or bullying"),
        (REASON_INAPPROPRIATE, "Inappropriate content"),
        (REASON_OTHER, "Something else"),
    ]

    STATUS_OPEN = "OPEN"
    STATUS_REVIEWED = "REVIEWED"
    STATUS_ACTION_TAKEN = "ACTION_TAKEN"
    STATUS_DISMISSED = "DISMISSED"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_ACTION_TAKEN, "Action taken"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="reports")
    # Null when the report targets the conversation/person as a whole rather
    # than one specific message (CC-006's card-level "Report" vs CC-010's
    # bubble-level "Report").
    message = models.ForeignKey(
        Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports"
    )
    reporter = models.ForeignKey(
        Participant, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports_filed"
    )
    # Who/what this report is ABOUT, as an identity_key ("L:.."/"T:.."/"S:..")
    # — deliberately separate from `reporter` (who FILED it). This is what
    # AdminResolveReportView's "suspend_user" action acts on. Populated at
    # creation time from the reported message's sender, or the DIRECT
    # thread's other participant when no specific message is targeted.
    target_identity = models.CharField(max_length=50, blank=True, default="", db_index=True)

    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    detail = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="chat_reports_resolved",
    )
    resolution_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["target_identity"]),
        ]

    def __str__(self):
        return f"Report<{self.reason}> on conv {self.conversation_id} [{self.status}]"


# ===========================================================================
# CHAT-LEVEL SUSPENSION  (Stage D · CC-023 admin "restrict user")
# ===========================================================================

class ChatSuspension(models.Model):
    """A chat-only suspension keyed on identity_key — deliberately NOT a
    field on accounts.Identity or accounts.User, so this stays a chat-app
    concern the same way Block does: gated in
    services.post_message_checked()/is_suspended(), enforced regardless of
    which endpoint (WS or REST) a send comes through, and lifted independent
    of any account-wide suspension a different admin tool might add later."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=50, unique=True, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    # Null = indefinite, lifted only by an explicit admin action.
    suspended_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="chat_suspensions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def is_active(self):
        return self.suspended_until is None or self.suspended_until > timezone.now()

    def __str__(self):
        return f"Suspension<{self.identity_key}> until={self.suspended_until or 'indefinite'}"


# ===========================================================================
# PER-IDENTITY COMMUNICATION PREFERENCES  (Stage E · CC-020/021)
# ===========================================================================

class CommPreference(models.Model):
    """Privacy toggles, keyed per-IDENTITY (not per-account like
    notifications.NotificationPreference) so child A and child B on one
    account can have different online-status/read-receipt settings, the
    same profile-isolation guarantee the rest of chat already gives for
    free. Lazily created on first read — see for_identity()."""
    identity_key = models.CharField(max_length=50, unique=True, db_index=True)
    show_online_status = models.BooleanField(default=True)
    show_read_receipts = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_identity(cls, identity_key):
        obj, _ = cls.objects.get_or_create(identity_key=identity_key)
        return obj

    def __str__(self):
        return f"CommPreference<{self.identity_key}>"


# ===========================================================================
# ACADEMIC SUPPORT TICKETS  (Stage D · CC-022, wires the reserved SUPPORT kind)
# ===========================================================================

class SupportTicket(models.Model):
    CATEGORY_TECHNICAL = "TECHNICAL"
    CATEGORY_BILLING = "BILLING"
    CATEGORY_COURSE = "COURSE"
    CATEGORY_ACCOUNT = "ACCOUNT"
    CATEGORY_OTHER = "OTHER"
    CATEGORY_CHOICES = [
        (CATEGORY_TECHNICAL, "Technical issue"),
        (CATEGORY_BILLING, "Billing / payments"),
        (CATEGORY_COURSE, "Course content"),
        (CATEGORY_ACCOUNT, "Account / access"),
        (CATEGORY_OTHER, "Other"),
    ]

    STATUS_OPEN = "OPEN"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_RESOLVED = "RESOLVED"
    STATUS_CLOSED = "CLOSED"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.OneToOneField(
        Conversation, on_delete=models.CASCADE, related_name="support_ticket"
    )
    requester_kind = models.CharField(max_length=10, choices=Participant.KIND_CHOICES)
    requester_learner = models.ForeignKey(
        LearnerProfile, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="support_tickets",
    )
    requester_teacher = models.ForeignKey(
        TeacherProfile, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="support_tickets",
    )
    subject = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_support_tickets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["status", "updated_at"])]

    def __str__(self):
        return f"Ticket<{self.subject}> [{self.status}]"
