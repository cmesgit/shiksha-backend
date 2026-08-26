"""
global_settings/models.py — one editable row of platform-wide settings.

This replaces the env-var ``PAYMENT_PROVIDER`` switch with a database row the
admin can flip live, with no server restart. It holds:

  * the payment mode (free / manual_upi / razorpay),
  * a master "free trial" toggle that forces everything free while on,
  * the platform UPI payee details (for the manual-UPI flow),
  * the Razorpay keys (used only when that mode is live),
  * the platform contact email (subscription / payment notifications).

Read it anywhere with ``GlobalSettings.load()`` — it always returns the single
row, creating it with safe defaults on first access. The table is a single row
keyed on ``pk=1``; ``save()`` enforces that.
"""
from datetime import timedelta

from django.db import models
from django.utils import timezone

from .fields import EncryptedCharField

# Flip these to True one at a time as each provider is fully implemented
# (backend flow + learner UI + verification path). Lives here (not in
# serializers.py) because it gates effective_mode's read-time computation too,
# not just the serializer's save-time validation — one source of truth for
# "is this payment mode actually safe to route real users into."
PAID_MODES_LIVE = {
    "manual_upi": False,
    "razorpay": False,
}


class GlobalSettings(models.Model):
    PAYMENT_FREE = "free"
    PAYMENT_MANUAL_UPI = "manual_upi"
    PAYMENT_RAZORPAY = "razorpay"
    PAYMENT_CHOICES = [
        (PAYMENT_FREE, "Free (no payment)"),
        (PAYMENT_MANUAL_UPI, "Manual UPI + admin approval"),
        (PAYMENT_RAZORPAY, "Razorpay gateway"),
    ]

    # Single-row table: the primary key is always 1 (enforced in save()).
    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )

    # ── Payment mode ──────────────────────────────────────────────────────
    payment_mode = models.CharField(
        max_length=12, choices=PAYMENT_CHOICES, default=PAYMENT_FREE,
        help_text="How money is collected when something is paid for.",
    )
    free_trial_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Master switch. While ON, the whole platform behaves as FREE "
            "(instant access) regardless of the payment mode above. Turn OFF "
            "to start charging using the selected payment mode. Also turns "
            "itself off automatically once the trial window below elapses — "
            "see trial_active / effective_mode."
        ),
    )
    trial_started_at = models.DateTimeField(
        default=timezone.now,
        help_text="When the current free-trial countdown began. Reset this "
                   "(e.g. to restart a 6-month trial from today) via the admin UI.",
    )
    trial_duration_days = models.PositiveIntegerField(
        default=180,
        help_text="Length of the free-trial window in days (default ~6 months).",
    )

    # ── Manual UPI (used when payment_mode = manual_upi) ──────────────────
    upi_id = models.CharField(
        max_length=120, blank=True,
        help_text="Platform VPA the student pays to, e.g. shiksha@okaxis",
    )
    upi_payee_name = models.CharField(max_length=120, blank=True)

    # ── Razorpay (used when payment_mode = razorpay) ──────────────────────
    razorpay_key_id = models.CharField(max_length=120, blank=True)
    # Encrypted at rest (Fernet) — see global_settings/fields.py. max_length is
    # sized for CIPHERTEXT, not the plaintext secret, since Fernet tokens are
    # substantially longer than their input.
    razorpay_key_secret = EncryptedCharField(max_length=500, blank=True)

    # ── Platform contact ──────────────────────────────────────────────────
    platform_email = models.EmailField(
        blank=True,
        help_text="Where subscription / payment notifications are sent.",
    )

    # ── Skill Dev pricing ladder ────────────────────────────────────────
    # Informational display values only while free_trial_enabled is True
    # (booking still charges 0 — see skills.payment_config_views). Tunable
    # here for when the free-launch phase ends.
    skill_intro_session_paise = models.PositiveIntegerField(
        default=9900,
        help_text="First-session-ever intro price with a given expert (₹99 default).",
    )
    skill_bundle_discount_pct = models.PositiveSmallIntegerField(
        default=15,
        help_text="Discount % off rate×remaining-sessions when buying the full track.",
    )

    # ── Live sessions ────────────────────────────────────────────────
    # Single source of truth for every /live room (instant meetings, group
    # sessions, and eventually course classes) — read exclusively through
    # sessions_app.live_rules so this admin panel is the only place limits
    # are set. See that module for how each field is used.
    live_free_minutes_per_join = models.PositiveIntegerField(
        default=15,
        help_text="Minutes a non-enrolled participant gets per join. Hosts are never capped.",
    )
    live_max_participants = models.PositiveIntegerField(
        default=40, help_text="Hard cap on participants per room, enforced at token issue."
    )
    live_max_session_minutes = models.PositiveIntegerField(
        default=90, help_text="Ceiling for the room's own duration, including host extensions."
    )
    live_daily_minutes_per_user = models.PositiveIntegerField(
        default=120, help_text="Daily budget per user. 0 disables the daily limit."
    )
    live_host_extensions_allowed = models.PositiveIntegerField(
        default=2, help_text="How many times a host may extend one session."
    )
    live_host_extension_minutes = models.PositiveIntegerField(
        default=15, help_text="Minutes added to the room's cap per host extension."
    )
    live_max_upload_mb = models.PositiveIntegerField(
        default=25, help_text="Max size of a single file shared inside a live session."
    )
    live_max_files_per_session = models.PositiveIntegerField(
        default=10, help_text="Max files shared inside one live session."
    )
    live_file_retention_days = models.PositiveIntegerField(
        default=2, help_text="Days shared files survive after the session ends."
    )
    live_recording_enabled = models.BooleanField(
        default=False, help_text="Master switch for session recording."
    )
    live_remote_access_enabled = models.BooleanField(
        default=True, help_text="Master switch for teacher screen-control of a student."
    )
    live_chat_enabled = models.BooleanField(
        default=True, help_text="Master switch for in-room chat."
    )
    live_screenshare_enabled = models.BooleanField(
        default=True, help_text="Master switch for screen sharing."
    )
    live_show_first_visit_tour = models.BooleanField(
        default=True, help_text="Show the first-visit product tour in the live room."
    )
    live_host_policy = models.CharField(
        max_length=24,
        default="teachers_and_enrolled",
        choices=[
            ("anyone", "Any signed-in user"),
            ("teachers_and_enrolled", "Teachers and enrolled learners"),
            ("teachers_only", "Teachers only"),
        ],
        help_text="Who may host (create/start) a live room.",
    )
    live_launch_free_mode = models.BooleanField(
        default=True,
        help_text="Launch promo: nobody is time-capped, whatever the values above say.",
    )

    # ── Product tours ──────────────────────────────────────────────────
    # Separate from live_show_first_visit_tour, which remains the live-room
    # sub-switch — see TOUR_SYSTEM_SPEC.md §4.3. Effective gate for the
    # live-room tour is tours_enabled AND live_show_first_visit_tour.
    tours_enabled = models.BooleanField(
        default=True, help_text="Master switch for all product tours across every app."
    )

    # ── Quiz system v2 (design_handoff_quiz_system) ─────────────────────
    # ai_question_drafting_enabled gates the existing "Generate with AI"
    # question-drafting path, which stays in the codebase but is
    # admin-controlled rather than always-on (PROMPT.md non-negotiable #6 —
    # the AI path itself is never removed, only fenced behind this switch).
    # It remains OFF by default, deliberately.
    #
    # quiz_v2_enabled is ON as of Phase 10 (migration 0008). ⚠ It gates
    # NOTHING: the rebuilt teacher builder, student hub, attempt and results
    # screens all shipped unconditionally, and the only mention of this flag
    # outside admin settings is the default shape in each app's AuthContext.
    # It is now a truthful record that v2 is the shipped system rather than a
    # switch — leaving it False said "v2 is off" while v2 was live, which is
    # worse than useless. Turning it off will NOT roll anything back; that
    # takes a deploy. Remove the field once a release has passed with no need
    # for it (BUILD_GUIDE Phase 10 item 3).
    quiz_v2_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Records that the redesigned quiz system is live. Gates nothing — "
            "the v2 screens ship unconditionally; turning this off does not "
            "revert them."
        ),
    )
    ai_question_drafting_enabled = models.BooleanField(
        default=False, help_text="Gate for the 'Generate with AI' question-drafting flow."
    )

    # ── Content Studio (design_handoff_content_studio) ──────────────────
    # Gates the restructured admin CMS — the four-group nav, the home
    # screen, the split page editor, Labels, Pictures and Exams — while it
    # is built out phase by phase (BUILD_GUIDE Phase 0 item 2). Unlike
    # quiz_v2_enabled above, this one is a REAL gate: every Content Studio
    # screen checks it, and with it OFF the existing eight-tab CMS is what
    # renders. It ships OFF and stays OFF until Phase 9 flips the default,
    # so a half-built Studio never reaches an admin mid-rebuild.
    content_studio_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Master switch for the restructured Content Studio CMS. While OFF, "
            "the remaining Content panel tabs are shown instead."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Global settings"
        verbose_name_plural = "Global settings"

    def __str__(self):
        return f"Global settings (mode={self.effective_mode})"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        super().save(*args, **kwargs)

    @property
    def trial_ends_at(self):
        return self.trial_started_at + timedelta(days=self.trial_duration_days)

    @property
    def trial_days_remaining(self):
        """Whole days left, floored at 0 (never negative once expired)."""
        remaining = self.trial_ends_at - timezone.now()
        return max(0, remaining.days)

    @property
    def trial_active(self):
        """Whether the countdown itself is still within its window.

        Distinct from ``free_trial_enabled`` (the manual override an admin can
        flip early) — this is purely date math, combined with the manual
        switch in ``effective_mode`` below.
        """
        return self.free_trial_enabled and timezone.now() < self.trial_ends_at

    @property
    def effective_mode(self):
        """The payment mode actually in force right now.

        Free while ``trial_active`` (manual switch ON and still inside the
        countdown window). Once the trial ends — by date or by the admin
        manually flipping the switch — falls through to ``payment_mode``, but
        ONLY if that mode is actually implemented end-to-end (PAID_MODES_LIVE).
        Neither manual_upi nor razorpay is wired yet, so this fails OPEN to
        free rather than silently routing real users into a payment flow that
        can't complete — the admin UI is responsible for surfacing "trial
        expired, no live payment method" so a human notices and acts.
        """
        if self.trial_active:
            return self.PAYMENT_FREE
        if PAID_MODES_LIVE.get(self.payment_mode, True):
            return self.payment_mode
        return self.PAYMENT_FREE

    @classmethod
    def load(cls):
        """Return the single settings row, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
