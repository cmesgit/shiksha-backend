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

CONVERSATION KINDS (v1):
  DIRECT  — 1:1 between exactly two participants (e.g. learner <-> teacher).
  COURSE  — a group room scoped to a course; participants are everyone
            enrolled (as their learner profile) plus the course's teacher(s).

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

from accounts.models import LearnerProfile, TeacherProfile


class Conversation(models.Model):
    KIND_DIRECT = "DIRECT"
    KIND_COURSE = "COURSE"
    KIND_CHOICES = [
        (KIND_DIRECT, "Direct (1:1)"),
        (KIND_COURSE, "Course room"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)

    # For COURSE rooms only. Soft reference (course lives in another app/db);
    # store the id and a denormalised title for display.
    course_id = models.UUIDField(null=True, blank=True)
    title = models.CharField(max_length=200, blank=True)

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
            models.UniqueConstraint(
                fields=["course_id"],
                condition=Q(kind="COURSE"),
                name="unique_course_conversation",
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "course_id"]),
        ]

    def group_name(self):
        """Channels group name for this conversation."""
        return f"chat_{self.id}"

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

    class Meta:
        indexes = [
            models.Index(fields=["blocker_learner"]),
            models.Index(fields=["blocker_teacher"]),
            models.Index(fields=["blocked_learner"]),
            models.Index(fields=["blocked_teacher"]),
        ]

    def __str__(self):
        return f"Block {self.pair_key}"
