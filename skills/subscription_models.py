"""
skills/subscription_models.py — the guest-expert advertising subscription.

A guest expert is LISTED in the directory for free. To be *advertised*
consistently (homepage promotion + a reach boost) they take a monthly
subscription. Cancelling / letting it lapse stops the promotion and decays
their reach (see ExpertProfile.is_advertised / decay_reach).

Phased billing (reusing GlobalSettings.effective_mode):
  • FREE phase  → subscribing is instant + free; everyone is advertised anyway.
  • PAID phase  → subscribing creates a pending record + a UPI instruction;
                  the expert submits the payment reference, an admin approves,
                  and that activates a 30-day period and flips is_featured on.

One evolving record per expert (OneToOne): we update its status/period rather
than spawning a new row each cycle, which keeps "their subscription" simple.

Additive — imported into skills/models.py so Django discovers it.
"""
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


# Default monthly advertising price (paise). Kept here as a constant so paid
# mode works without another migration; move to GlobalSettings later if needed.
SKILL_AD_MONTHLY_PAISE = 49900   # ₹499 / month
SKILL_AD_PERIOD_DAYS = 30

# Reach adjustments tied to the advertising lifecycle.
REACH_ACTIVATION_BOOST = 100     # added when a paid period activates
REACH_SESSION_BUMP = 5           # added per completed session (organic)
REACH_CANCEL_FACTOR = 0.5        # reach multiplier when advertising stops


class ExpertAdSubscription(models.Model):
    PLAN_FREE = "free"
    PLAN_MONTHLY = "monthly"
    PLAN_CHOICES = [
        (PLAN_FREE, "Free (launch period)"),
        (PLAN_MONTHLY, "Monthly"),
    ]

    STATUS_PENDING = "pending"      # created, awaiting payment proof
    STATUS_SUBMITTED = "submitted"  # proof submitted, awaiting admin approval
    STATUS_ACTIVE = "active"        # advertising live
    STATUS_CANCELLED = "cancelled"  # turned off by the expert
    STATUS_EXPIRED = "expired"      # period elapsed without renewal
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_SUBMITTED, "Payment submitted"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    expert = models.OneToOneField(
        "skills.ExpertProfile",
        on_delete=models.CASCADE,
        related_name="ad_subscription",
    )

    plan = models.CharField(max_length=10, choices=PLAN_CHOICES, default=PLAN_MONTHLY)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    amount = models.PositiveIntegerField(default=SKILL_AD_MONTHLY_PAISE, help_text="Paise")
    auto_renew = models.BooleanField(default=True)

    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # Manual-UPI proof (paid mode), mirroring SkillPaymentRequest's shape.
    upi_reference = models.CharField(max_length=40, blank=True)
    payer_vpa = models.CharField(max_length=120, blank=True)
    receipt = models.ImageField(
        upload_to="skills/ad_subscriptions/receipts/", null=True, blank=True
    )
    note = models.TextField(blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_ad_subscriptions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["status"], name="skills_expe_status_idx")]

    # ── lifecycle ──────────────────────────────────────────────────────────
    def is_currently_active(self):
        if self.status != self.STATUS_ACTIVE:
            return False
        if self.current_period_end is None:
            return True
        return timezone.now() <= self.current_period_end

    def activate(self, *, days=SKILL_AD_PERIOD_DAYS, free=False, reviewer=None):
        """Start (or extend) an active period and turn advertising on."""
        now = timezone.now()
        base = (
            self.current_period_end
            if (self.current_period_end and self.current_period_end > now)
            else now
        )
        self.plan = self.PLAN_FREE if free else self.PLAN_MONTHLY
        self.status = self.STATUS_ACTIVE
        self.current_period_start = self.current_period_start or now
        self.current_period_end = None if free else (base + timedelta(days=days))
        self.started_at = self.started_at or now
        self.cancelled_at = None
        if reviewer is not None:
            self.reviewed_by = reviewer
            self.reviewed_at = now
        self.save()

        ep = self.expert
        ep.is_featured = True
        ep.featured_since = ep.featured_since or now
        ep.reach_count = (ep.reach_count or 0) + (0 if free else REACH_ACTIVATION_BOOST)
        ep.save(update_fields=["is_featured", "featured_since", "reach_count", "updated_at"])
        return self

    def cancel(self):
        """Turn advertising off and decay reach (per the product rule)."""
        now = timezone.now()
        self.status = self.STATUS_CANCELLED
        self.auto_renew = False
        self.cancelled_at = now
        self.save(update_fields=["status", "auto_renew", "cancelled_at", "updated_at"])

        ep = self.expert
        ep.is_featured = False
        ep.featured_since = None
        ep.reach_count = int((ep.reach_count or 0) * REACH_CANCEL_FACTOR)
        ep.save(update_fields=["is_featured", "featured_since", "reach_count", "updated_at"])
        return self

    def __str__(self):
        return f"AdSub · {self.expert.display_name()} · {self.status}"
