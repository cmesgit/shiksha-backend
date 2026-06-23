"""
skills/payment_models.py — manual-UPI payment requests for the skill marketplace.

When the platform runs in ``manual_upi`` mode, a learner who books a paid
session or buys a paid skill course pays the platform UPI ID (from
GlobalSettings), then submits this record with the UPI reference (UTR) and an
optional receipt screenshot. An admin verifies and approves it, which is what
unlocks the session / course.

Additive — imported into skills/models.py so Django discovers it.
"""
import uuid

from django.conf import settings
from django.db import models


class SkillPaymentRequest(models.Model):
    PURPOSE_SESSION = "session"
    PURPOSE_COURSE = "course"
    PURPOSE_CHOICES = [
        (PURPOSE_SESSION, "Session booking"),
        (PURPOSE_COURSE, "Course purchase"),
    ]

    STATUS_SUBMITTED = "submitted"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="skill_payment_requests",
    )
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES)

    # Exactly one of these is set, depending on `purpose`.
    session = models.ForeignKey(
        "skills.SkillSession",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="payment_requests",
    )
    course = models.ForeignKey(
        "skills.SkillCourse",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="payment_requests",
    )

    amount = models.PositiveIntegerField(default=0, help_text="Paise (₹1 = 100)")

    # Proof submitted by the learner.
    upi_reference = models.CharField(
        max_length=40, blank=True, help_text="UTR / UPI transaction reference"
    )
    payer_vpa = models.CharField(
        max_length=120, blank=True, help_text="Payer's UPI ID (optional)"
    )
    receipt = models.ImageField(
        upload_to="skills/payments/receipts/", null=True, blank=True
    )
    note = models.TextField(blank=True)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_SUBMITTED
    )
    reject_reason = models.TextField(blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_skill_payments",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "purpose"])]

    @property
    def amount_rupees(self):
        return self.amount // 100

    def __str__(self):
        return f"PaymentRequest · {self.purpose} · {self.status}"
