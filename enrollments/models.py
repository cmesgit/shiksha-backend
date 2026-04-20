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
        ]

    def __str__(self):
        return f"{self.user.email} → {self.course.title}"
