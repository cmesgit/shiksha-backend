import uuid
from django.db import models
from django.conf import settings


class EnrollmentRequest(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    METHOD_UPI = "UPI"
    METHOD_BANK = "BANK"

    METHOD_CHOICES = [
        (METHOD_UPI, "UPI"),
        (METHOD_BANK, "Bank Transfer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollment_requests",
    )

    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.PROTECT,
        related_name="enrollment_requests",
    )

    amount_paid = models.PositiveIntegerField(help_text="Amount claimed by student, in paise")

    payment_method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=METHOD_UPI)
    utr_number = models.CharField(max_length=30)
    payment_date = models.DateField()

    receipt = models.ImageField(upload_to="enrollment_receipts/%Y/%m/")

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    admin_note = models.TextField(blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_enrollment_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["status", "-submitted_at"]),
            models.Index(fields=["user", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "course"],
                condition=models.Q(status="PENDING"),
                name="unique_pending_request_per_user_course",
            ),
        ]

    def __str__(self):
        return f"{self.user.email} → {self.course.title} [{self.status}]"


class Enrollment(models.Model):
    STATUS_ACTIVE = "ACTIVE"
    STATUS_REVOKED = "REVOKED"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_REVOKED, "Revoked"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollments",
    )

    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.CASCADE,
        related_name="enrollments",
    )

    # NEW: structured batch assignment.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enrollments",
    )

    # LEGACY: kept only so the data migration can backfill `batch`.
    # Delete this field (and run a migration) once 0003 has run and you've
    # confirmed every row has a `batch` set where it should.
    batch_code = models.CharField(max_length=30, null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )

    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "course")
        indexes = [
            models.Index(fields=["user", "course"]),
            models.Index(fields=["status"]),
            models.Index(fields=["batch", "status"]),  # "active students in A13"
        ]

    def __str__(self):
        return f"{self.user.email} → {self.course.title}"


class Subscription(models.Model):
    STATUS_ACTIVE = "ACTIVE"
    STATUS_EXPIRED = "EXPIRED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    KIND_TRIAL = "TRIAL"
    KIND_PAID = "PAID"

    KIND_CHOICES = [
        (KIND_TRIAL, "Trial"),
        (KIND_PAID, "Paid"),
    ]

    TRIAL_DURATION_DAYS = 30

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )

    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )

    kind = models.CharField(
        max_length=10,
        choices=KIND_CHOICES,
        default=KIND_PAID,
        help_text="TRIAL = free 30-day trial; PAID = approved enrollment.",
    )

    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )

    source_request = models.ForeignKey(
        EnrollmentRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscriptions",
    )

    # --- Trial lifecycle nudge tracking (idempotent email sends) ---
    trial_reminder_7d_sent_at = models.DateTimeField(null=True, blank=True)
    trial_reminder_2d_sent_at = models.DateTimeField(null=True, blank=True)
    trial_ended_email_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expires_at"]
        indexes = [
            models.Index(fields=["user", "course", "status"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["kind", "status", "expires_at"]),
        ]
        constraints = [
            # A user gets exactly one trial per course, ever.
            models.UniqueConstraint(
                fields=["user", "course"],
                condition=models.Q(kind="TRIAL"),
                name="unique_trial_per_user_course",
            ),
        ]

    def __str__(self):
        return f"{self.user.email} → {self.course.title} [{self.kind}/{self.status} until {self.expires_at:%Y-%m-%d}]"

    @property
    def is_currently_active(self):
        from django.utils import timezone
        return self.status == self.STATUS_ACTIVE and self.expires_at > timezone.now()

    @property
    def is_trial(self):
        return self.kind == self.KIND_TRIAL

    @property
    def days_remaining(self):
        from django.utils import timezone
        delta = self.expires_at - timezone.now()
        return max(0, delta.days)
