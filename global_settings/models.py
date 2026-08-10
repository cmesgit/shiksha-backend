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
