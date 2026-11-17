"""Community verification of Explore uploads.

    Every upload goes live as Unverified. It becomes Verified when 3 members
    who specialise in one of its tags confirm it is accurate.

Spec: `shiksha-backend/docs/EXPLORE_VERIFICATION.md`. This module holds the
state; `documents/verification.py` holds the rules that move between states.

⚠ This is NOT the Research Hub. `ResearchReviewer` / `ResearchSubmission` in
`documents/models.py` are a formal, admin-brokered peer review of research
papers, with private manuscripts that never touch public storage. This is the
open, community-run check on ordinary library uploads, and research papers are
routed away from it. The two share no models and no permission rules; naming
one after the other is the easiest way to confuse them, so nothing here is
called a "reviewer" without "verification" nearby.

Imported into `documents.models`' namespace at the bottom of that file, so
`from .models import VerificationReview` works like every other model here.
"""
from django.conf import settings
from django.db import models

from .models import Document


class VerificationStatus(models.TextChoices):
    """The four states a set can be in.

    ⚠ `CHANGES_REQUESTED` and `HIDDEN` are NOT the same thing, and the
    difference is the whole point of the change-request flow:

      * CHANGES_REQUESTED — one specialist said no. The set **stays in
        Browse**, still showing the public an Unverified badge, with its
        confirmations frozen. The uploader is told what to fix.
      * HIDDEN — two specialists said no on the same version. Only then does
        the set leave Browse, and an admin has to look at it.

    Treating the first as a takedown would let a single reviewer remove anyone's
    upload from the library.
    """

    UNVERIFIED = "unverified", "Unverified"
    VERIFIED = "verified", "Verified"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"
    HIDDEN = "hidden", "Hidden pending review"


class SpecializationSource(models.TextChoices):
    TEACHER = "teacher", "Teacher account"
    EARNED = "earned", "Earned from verified uploads"
    APPROVED = "approved", "Approved request"


class SpecializationStatus(models.TextChoices):
    PENDING = "pending", "Awaiting approval"
    CONFIRMED = "confirmed", "Confirmed"
    DECLINED = "declined", "Declined"


class SubjectSpecialization(models.Model):
    """A member's claim to review in one subject.

    Three ways in, all switchable by an admin (README §5a, "Who can review"):
    a teacher is confirmed in the subjects they teach with no extra step;
    anyone else is confirmed once N of their own uploads in that subject are
    verified; or they ask and an admin decides.

    ⚠ `PENDING` is not a weak form of confirmed. Only `CONFIRMED` rows make a
    review count, and the distinction is the only thing standing between "an
    admin approved this person for this subject" and "this person typed the
    subject into a box".
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="verification_specializations",
    )
    subject = models.CharField(max_length=120)
    source = models.CharField(
        max_length=16, choices=SpecializationSource.choices,
        default=SpecializationSource.APPROVED,
    )
    status = models.CharField(
        max_length=16, choices=SpecializationStatus.choices,
        default=SpecializationStatus.PENDING,
    )
    # Free text the member supplies with a request — "M.Tech Structural
    # Engineering, NIT Silchar." The design shows it to the admin verbatim as
    # the whole basis for approving, so it is never truncated for display.
    evidence = models.TextField(blank=True, default="")
    # Paused reviewers keep their specialization but stop being invited. A
    # separate flag rather than a fourth status, because pausing is reversible
    # from the admin's Reviewers tab and must not look like a decline.
    is_paused = models.BooleanField(default=False)
    missed_invites = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verification_specializations_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["subject"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "subject"],
                name="documents_unique_user_subject_specialization",
            ),
        ]
        indexes = [
            # The hot query is "who is confirmed in this subject", for
            # matching, not "what is this person confirmed in".
            models.Index(fields=["subject", "status"],
                         name="docspec_subject_status_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} · {self.subject} ({self.status})"


class ReviewOutcome(models.TextChoices):
    CONFIRMED = "confirmed", "Confirmed as accurate"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"


class VerificationReview(models.Model):
    """One specialist's answers to the three checks, on one version of a set.

    The three questions are fixed copy from the design and are stored as three
    booleans rather than a score, because the uploader is shown *which* check
    failed.

    ⚠ `version` is what makes two of the spec's rules work at all:

      * "Uploading a new version clears earlier confirmations and restarts the
        count" — the old reviews stay (they are a record of what someone
        actually said) but stop counting, because the count is per version.
      * "Each member reviews a version once" — enforced by the unique
        constraint below, in the database. A view-level check races: two
        submits from a double-clicked button arrive concurrently, both read
        zero existing reviews, and both write.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="verification_reviews",
    )
    version = models.PositiveIntegerField()
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="verification_reviews",
    )
    # The subject the reviewer was acting as a specialist in. Recorded rather
    # than recomputed: a specialization can be revoked later, and the history
    # has to keep saying what it said at the time.
    subject = models.CharField(max_length=120, blank=True, default="")

    matches_tags = models.BooleanField()
    content_accurate = models.BooleanField()
    files_complete = models.BooleanField()
    # Optional when every answer is yes, REQUIRED when any is no — the design
    # relabels the field and the button together. Enforced in the serializer,
    # because "required" here depends on the other three values.
    note = models.TextField(blank=True, default="")

    outcome = models.CharField(max_length=20, choices=ReviewOutcome.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version", "reviewer"],
                name="documents_unique_review_per_version",
            ),
        ]
        indexes = [
            models.Index(fields=["document", "version"],
                         name="docreview_doc_version_idx"),
        ]

    @property
    def is_confirmation(self):
        return self.outcome == ReviewOutcome.CONFIRMED

    def __str__(self):
        return f"{self.document_id} v{self.version} by {self.reviewer_id}"


class InviteState(models.TextChoices):
    OPEN = "open", "Awaiting a response"
    ANSWERED = "answered", "Review submitted"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"
    # The set moved on — a new version, or it reached Verified without this
    # reviewer. Distinct from EXPIRED so "missed invites in a row" does not
    # punish a reviewer for an invite that stopped being answerable.
    SUPERSEDED = "superseded", "No longer needed"


class VerificationInvite(models.Model):
    """An ask to one specialist to review one version of one set.

    Only exists under the "Invite automatically" and "Admin assigns" policies.
    Under "Open queue" there are no invites and no deadlines — every eligible
    set is simply in every eligible specialist's queue.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="verification_invites",
    )
    version = models.PositiveIntegerField()
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="verification_invites",
    )
    subject = models.CharField(max_length=120, blank=True, default="")
    state = models.CharField(
        max_length=16, choices=InviteState.choices, default=InviteState.OPEN,
    )
    # Set by an admin from the Needs attention tab rather than by matching.
    # Worth distinguishing: an admin assignment should not be expired out from
    # under them by the same deadline sweep that rotates automatic invites.
    assigned_by_admin = models.BooleanField(default=False)
    invited_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # ⚠ Set ONLY by the reminder sweep. It is surfaced to the queue row as
    # "· reminder sent", so anything else that writes it makes the UI state a
    # falsehood. "Mark all as read" uses read_at below for exactly that
    # reason — it used to borrow this field and forge the claim.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-invited_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version", "reviewer"],
                name="documents_unique_invite_per_version",
            ),
        ]
        indexes = [
            models.Index(fields=["reviewer", "state"],
                         name="docinvite_reviewer_state_idx"),
            # The expiry sweep reads this.
            models.Index(fields=["state", "expires_at"],
                         name="docinvite_state_expiry_idx"),
        ]

    def __str__(self):
        return f"invite {self.document_id} v{self.version} → {self.reviewer_id}"


class ReviewerSelection(models.TextChoices):
    OPEN_QUEUE = "open_queue", "Open queue"
    AUTOMATIC = "automatic", "Invite automatically"
    ADMIN_ASSIGNS = "admin_assigns", "Admin assigns"


class VerificationSettings(models.Model):
    """The admin's knobs, as a singleton row. README §5a is the spec.

    ⚠ Deliberately NOT fields on `GlobalSettings`. That model carries the
    Razorpay key id, the platform UPI payee and VPA, the contact email and the
    whole live-session rule set, which is exactly why `PublicConfigView` is a
    hand-built allowlist rather than a serializer dump. Nineteen more fields
    there is nineteen more chances for one of them to be published by accident.
    These are their own table with their own endpoint.

    Defaults are the design's defaults, not guesses — the admin screen opens on
    these values and its summary box reads back the flow they describe.
    """

    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False,
    )

    # ── Reaching Verified ────────────────────────────────────────────────
    confirmations_needed = models.PositiveSmallIntegerField(default=3)
    # OFF means unverified uploads are not public at all. That inverts the
    # whole feature from "a badge" into "a gate", so it is a real decision and
    # the admin screen says so in the consequence line.
    show_unverified_in_browse = models.BooleanField(default=True)
    change_requests_before_hidden = models.PositiveSmallIntegerField(default=2)

    # ── Assigning reviewers ──────────────────────────────────────────────
    reviewer_selection = models.CharField(
        max_length=16, choices=ReviewerSelection.choices,
        default=ReviewerSelection.AUTOMATIC,
    )
    reviewers_invited_per_set = models.PositiveSmallIntegerField(default=3)
    days_to_respond = models.PositiveSmallIntegerField(default=7)
    notify_admins_when_no_reviewer = models.BooleanField(default=True)

    # ── Automatic matching ───────────────────────────────────────────────
    widen_to_same_field = models.BooleanField(default=True)
    prefer_same_branch = models.BooleanField(default=True)
    skip_uploader_institution = models.BooleanField(default=True)
    open_reviews_per_reviewer = models.PositiveSmallIntegerField(default=5)
    remind_invited_reviewers = models.BooleanField(default=True)
    reminder_days_before = models.PositiveSmallIntegerField(default=2)
    pause_inactive_reviewers = models.BooleanField(default=True)
    missed_invites_before_pause = models.PositiveSmallIntegerField(default=3)

    # ── Who can review ───────────────────────────────────────────────────
    teachers_review_their_subjects = models.BooleanField(default=True)
    members_can_earn_it = models.BooleanField(default=True)
    verified_uploads_to_earn_it = models.PositiveSmallIntegerField(default=2)
    members_can_request_it = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Verification settings"
        verbose_name_plural = "Verification settings"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        # ⚠ Pinning the pk alone does NOT make a singleton — it makes every
        # second write a duplicate-key error. `GlobalSettings` gets away with
        # that only because every caller happens to go through `load()`.
        # Here the admin screen PATCHes these settings, so a constructed-
        # then-saved instance is a realistic path, and an IntegrityError is
        # not something the caller can act on. Writing it as an update keeps
        # the table a singleton in fact and not just by convention.
        if self._state.adding and type(self).objects.filter(pk=1).exists():
            self._state.adding = False
            kwargs.pop("force_insert", None)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The single settings row, created with the design's defaults if
        absent. Same contract as ``GlobalSettings.load()``."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Verification settings (needs {self.confirmations_needed})"
