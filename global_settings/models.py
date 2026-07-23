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
from django.db import models

from .fields import EncryptedCharField


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
            "to start charging using the selected payment mode."
        ),
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
    def effective_mode(self):
        """The payment mode actually in force right now.

        The free-trial master switch wins: while it is on, everything is free
        no matter what ``payment_mode`` says.
        """
        return self.PAYMENT_FREE if self.free_trial_enabled else self.payment_mode

    @classmethod
    def load(cls):
        """Return the single settings row, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
