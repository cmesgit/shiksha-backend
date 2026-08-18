"""
accounts/settings_models.py — models backing the Settings surface.

Imported at the bottom of accounts/models.py so Django registers them with the
`accounts` app label (same pattern as skills/payment_models.py).

Four things the Settings redesign (and, later, the tour system) need that the
schema had no home for:

  * UserSession           — "Sessions & devices": one row per browser/app the
                            account is signed in on, so a session can be listed
                            and revoked individually.
  * LearningGoal          — "Learning goals": a learner's daily study target,
                            active weekdays and reminder time. Per LearnerProfile,
                            not per account: two children on one email keep
                            separate habits.
  * AccountDeletionRequest — "Privacy & data": the audit row for a self-serve
                            account closure, and the queue the purge job reads.
  * TourState             — which product tours a given IDENTITY (learner
                            profile or teacher) has seen. See TOUR_SYSTEM_SPEC.md
                            §4.1 for the full design.

WHY UserSession EXISTS AT ALL
─────────────────────────────
simplejwt's OutstandingToken already enumerates live refresh tokens, but it
carries no device/IP columns and — more importantly — a *single* browser mints
many refresh tokens over its life (every profile switch, track switch and hourly
rotation calls build_tokens()). Keying "a session" on the token would show one
browser as a dozen sessions. So a stable UserSession id is minted once at login
and carried through every subsequent rotation as the `sid` JWT claim.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


# ─────────────────────────────────────────────────────────────────────────────
# Sessions & devices
# ─────────────────────────────────────────────────────────────────────────────

class UserSession(models.Model):
    """One signed-in browser / app install, identified by the `sid` JWT claim.

    Lifecycle: created by LoginView, `last_active_at` bumped on every token
    rotation (login / profile select / track switch / refresh), and closed by
    logout or an explicit revoke. Rows are kept after revocation so the audit
    trail survives — `revoked_at` is the liveness flag, not deletion.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sessions",
    )

    # Raw UA kept verbatim for support/forensics; the three *_label fields are
    # the parsed form the UI renders ("Chrome · Windows"). Parsing happens once
    # at creation (accounts/device.py) rather than on every list request.
    user_agent = models.TextField(blank=True)
    browser_label = models.CharField(max_length=60, blank=True)
    platform_label = models.CharField(max_length=60, blank=True)
    device_kind = models.CharField(
        max_length=10,
        choices=[("desktop", "Desktop"), ("mobile", "Mobile"), ("tablet", "Tablet")],
        default="desktop",
    )

    # No geo-IP database is deployed, so the UI shows the IP itself rather than
    # inventing a city. Nullable because a request can arrive without one.
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_active_at"]
        indexes = [
            models.Index(fields=["user", "revoked_at"], name="idx_usersession_user_live"),
        ]

    def __str__(self):
        return f"{self.label} · {self.user_id}"

    @property
    def is_live(self):
        return self.revoked_at is None

    @property
    def label(self):
        parts = [p for p in (self.browser_label, self.platform_label) if p]
        return " · ".join(parts) or "Unknown device"

    def revoke(self):
        """Idempotent — revoking an already-revoked session is a no-op so a
        double-click on the UI's Revoke button can't move the timestamp."""
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Learning goals
# ─────────────────────────────────────────────────────────────────────────────

class LearningGoal(models.Model):
    """A learner's study habit settings. One row per LearnerProfile.

    The streak is NOT stored here — it's derived from activity rows at read
    time (see accounts/settings_views.py), so it can never drift out of sync
    with what the learner actually did.
    """

    # Mon=0 … Sun=6, matching Python's date.weekday() so the streak logic and
    # this list index the same way with no translation layer.
    DEFAULT_ACTIVE_DAYS = [0, 1, 2, 3, 4]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.OneToOneField(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="learning_goal",
    )

    daily_minutes = models.PositiveSmallIntegerField(default=30)
    active_days = models.JSONField(default=list, blank=True)
    reminder_time = models.TimeField(null=True, blank=True)
    reminders_enabled = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Goal<{self.profile_id}> {self.daily_minutes}min"

    @classmethod
    def for_profile(cls, profile):
        """Lazily create with sane defaults, mirroring
        notifications.NotificationPreference.for_user() so both preference
        tables behave the same way on first read."""
        obj, _ = cls.objects.get_or_create(
            profile=profile,
            defaults={"active_days": list(cls.DEFAULT_ACTIVE_DAYS)},
        )
        return obj


# ─────────────────────────────────────────────────────────────────────────────
# Account deletion
# ─────────────────────────────────────────────────────────────────────────────

class AccountDeletionRequest(models.Model):
    """A password-confirmed request to close an account.

    Deliberately NOT an immediate hard delete. The account is deactivated and
    every session revoked the moment this row is written — from the user's point
    of view it is gone — but the rows survive a grace window so that:

      * an accidental or hostile-takeover deletion can be reversed by support;
      * cascades into enrollments / chat / forum / documents are not fired
        inside a web request;
      * payment and attendance records stay available for the retention period
        they're legally held for.

    `purge_after` is when a purge job may hard-delete. Nothing purges
    automatically yet — see accounts/settings_views.py for the note.
    """

    GRACE_DAYS = 30

    STATUS_PENDING = "pending"
    STATUS_CANCELLED = "cancelled"
    STATUS_PURGED = "purged"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending purge"),
        (STATUS_CANCELLED, "Cancelled (account restored)"),
        (STATUS_PURGED, "Purged"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="deletion_requests",
    )
    # Kept denormalized: after a purge the User row is gone but the audit row
    # should still say which address asked to be forgotten.
    email = models.EmailField(blank=True)
    reason = models.CharField(max_length=300, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    purge_after = models.DateTimeField()
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status", "purge_after"], name="idx_acctdel_due"),
        ]

    def __str__(self):
        return f"DeletionRequest<{self.email}> {self.status}"


# ─────────────────────────────────────────────────────────────────────────────
# Product tours
# ─────────────────────────────────────────────────────────────────────────────

class TourState(models.Model):
    """
    Per-IDENTITY record of which product tours this person has seen.

    Keyed on identity_key ("L:<uuid>" / "T:<uuid>") rather than on User, for the
    same reason chat.CommPreference is: one account owns several LearnerProfiles
    plus possibly a TeacherProfile, and each sees a materially different UI. A
    tour is a learned-UI fact about a person, not about a billing account — the
    second child on a shared email has not "already seen" the dashboard tour.

    `tours` is a JSON map rather than a row per tour because the client wants the
    whole map in one request at boot and the key count is bounded (<30). Same
    trade-off already made by NotificationPreference.muted_categories and
    LearningGoal.active_days.

        {"student.welcome.academy": {
            "status": "completed",   # completed | dismissed
            "version": 3,            # registry version at the time
            "step": 4,               # furthest step reached (drop-off analytics)
            "at": "2026-08-17T...Z",
            "count": 1               # times started
        }}
    """
    STATUS_COMPLETED = "completed"
    STATUS_DISMISSED = "dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=64, unique=True, db_index=True)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tour_states"
    )

    tours = models.JSONField(default=dict, blank=True)

    # Rule-engine state (TOUR_SYSTEM_SPEC.md §5)
    autoplay_enabled = models.BooleanField(default=True)
    last_auto_tour_at = models.DateTimeField(null=True, blank=True)
    consecutive_dismissals = models.PositiveSmallIntegerField(default=0)

    # Absence detection (TOUR_SYSTEM_SPEC.md §6.3)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    prev_seen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["identity_key"], name="idx_tourstate_identity")]

    def __str__(self):
        return f"TourState<{self.identity_key}>"

    @classmethod
    def for_identity(cls, identity_key, account):
        obj, _ = cls.objects.get_or_create(
            identity_key=identity_key, defaults={"account": account}
        )
        return obj
