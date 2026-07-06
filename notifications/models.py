# PLACEMENT: backend/backend/notifications/models.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/models.py
#
# The ONE site-wide notification table. Every app (forum, counseling,
# assignments, enrollments, payments, ...) writes here through
# notifications.services.notify() — never directly.
#
# Design notes
# ────────────
# • pk is a BigAutoField (int), NOT a UUID, so the legacy forum routes
#   (`<int:notification_id>/read/`) keep working unchanged.
# • `verb` is a dotted, app-prefixed event name: "forum.reply",
#   "forum.upvote", "counseling.booked", "assignment.graded". Filtering a
#   product area is `verb__startswith="forum."`.
# • `payload` carries whatever ids the frontend needs to deep-link
#   (thread_id, appointment_id, ...). `link_url` is the pre-computed click
#   target so the bell component never needs per-verb routing logic.
# • `audience_role` scopes a notification to one ROLE of a multi-role
#   account (STUDENT vs TEACHER dashboard). It is NOT precise enough for
#   profile isolation: two dependent children on one account are both
#   STUDENT, so a STUDENT-scoped row shows on BOTH their dashboards. That
#   was the child-A/child-B notification leak.
# • `audience_identity` (M2 — Phase 3 §18) is the precise fix: an identity
#   key ("L:<learner_uuid>" / "T:<teacher_id>" — the exact string format of
#   accounts.Identity.key and chat.Participant.identity_key()). A
#   counseling.booked for one child carries that child's identity key and
#   reaches only that child's dashboard. Blank = account-wide (every
#   identity on the account), same "show everywhere" meaning audience_role
#   blank always had. audience_role is retained during the transition and
#   is derivable from the identity's kind, so nothing that still sends only
#   a role breaks.
# • `actor` is SET_NULL: deleting a user must not erase other people's
#   notification history (the old forum model CASCADEd on sender — a
#   deleted spammer silently deleted his victims' notifications too).

from django.conf import settings
from django.db import models


class Notification(models.Model):
    ROLE_STUDENT = "STUDENT"
    ROLE_TEACHER = "TEACHER"
    ROLE_ADMIN = "ADMIN"
    ROLE_COUNSELOR = "COUNSELOR"

    AUDIENCE_ROLE_CHOICES = [
        (ROLE_STUDENT, "Student"),
        (ROLE_TEACHER, "Teacher"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_COUNSELOR, "Counselor"),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acted_notifications",
    )

    verb = models.CharField(max_length=50, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    link_url = models.CharField(max_length=500, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    audience_role = models.CharField(
        max_length=20,
        choices=AUDIENCE_ROLE_CHOICES,
        blank=True,
        default="",
        help_text="Optional. Restrict to one dashboard ROLE; blank = all. "
                  "Coarse — prefer audience_identity for profile isolation.",
    )
    audience_identity = models.CharField(
        max_length=50,
        blank=True,
        default="",
        db_index=True,
        help_text='Optional. Restrict to ONE identity, e.g. "L:<uuid>" / '
                  '"T:<id>" (accounts.Identity.key format). Blank = every '
                  "identity on the account. This is the precise per-profile "
                  "scope; audience_role is the coarse legacy fallback.",
    )

    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["recipient", "created_at"]),
            # M2: dashboards query "rows for my identity + account-wide rows".
            models.Index(fields=["recipient", "audience_identity"]),
        ]

    def __str__(self):
        return f"[{self.verb}] → {self.recipient}: {self.title}"
