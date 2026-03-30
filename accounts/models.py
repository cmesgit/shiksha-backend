import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.auth.models import AbstractUser
from django.db import models


# =====================================================
# USER
# =====================================================

class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True)

    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        return self.email

    # 🔐 Safe role checker (ONLY use Role constants)
    def has_role(self, role_name):
        return self.user_roles.filter(
            role__name=role_name,
            is_active=True
        ).exists()

    def get_active_roles(self):
        return list(
            self.user_roles.filter(is_active=True)
            .values_list("role__name", flat=True)
        )


# =====================================================
# PROFILE
# =====================================================

class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    full_name = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    student_id = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True
    )

    avatar_image = models.ImageField(
        upload_to="avatar/",
        null=True,
        blank=True
    )

    avatar_emoji = models.CharField(
        max_length=10,
        blank=True,
        null=True
    )

    # Form Fillup fields
    date_of_birth = models.DateField(null=True, blank=True)
    father_name = models.CharField(max_length=150, blank=True)
    father_phone = models.CharField(max_length=15, blank=True)
    mother_name = models.CharField(max_length=150, blank=True)
    guardian = models.CharField(max_length=150, blank=True)
    guardian_phone = models.CharField(max_length=15, blank=True)
    current_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    same_as_current = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.same_as_current:
            self.permanent_address = self.current_address
        super().save(*args, **kwargs)

    def avatar_type(self):
        if self.avatar_image:
            return "image"
        if self.avatar_emoji:
            return "emoji"
        return None

    def avatar_value(self):
        if self.avatar_image:
            return self.avatar_image.url
        if self.avatar_emoji:
            return self.avatar_emoji
        return None

    def __str__(self):
        return f"{self.user.email} Profile"

    @property
    def is_complete(self):
        return bool(
            self.full_name
            and self.phone
            and self.date_of_birth
            and self.father_name
            and self.mother_name
            and self.current_address
            and (self.permanent_address or self.same_as_current)
            and self.user.is_verified
        )


# =====================================================
# ROLE (STRICT)
# =====================================================

class Role(models.Model):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    ADMIN = "ADMIN"
    GUEST = "GUEST"

    ROLE_CHOICES = [
        (STUDENT, "Student"),
        (TEACHER, "Teacher"),
        (ADMIN, "Admin"),
        (GUEST, "Guest"),
    ]

    name = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        unique=True
    )

    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# =====================================================
# USER ROLE (HARDENED)
# =====================================================

class UserRole(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_roles",
    )

    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    is_active = models.BooleanField(default=True)

    # 🔐 Only ONE primary role per user
    is_primary = models.BooleanField(default=False)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_roles",
    )

    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "role")
        indexes = [
            models.Index(fields=["user", "is_active"]),
        ]

    def clean(self):
        """
        Enforce:
        - Only ONE primary role per user
        - Only ONE active role per user (strict mode)
        """

        if self.is_primary:
            existing_primary = UserRole.objects.filter(
                user=self.user,
                is_primary=True
            ).exclude(pk=self.pk)

            if existing_primary.exists():
                raise ValidationError("User already has a primary role.")

        if self.is_active:
            existing_active = UserRole.objects.filter(
                user=self.user,
                is_active=True
            ).exclude(pk=self.pk)

            if existing_active.exists():
                raise ValidationError("User already has an active role.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def approve(self, admin_user):
        self.is_active = True
        self.approved_by = admin_user
        self.approved_at = timezone.now()
        self.save()

    def __str__(self):
        return f"{self.user.email} → {self.role.name}"


# =====================================================
# AUTH EVENT (AUDIT SAFE)
# =====================================================

class AuthEvent(models.Model):
    EVENT_LOGIN_SUCCESS = "LOGIN_SUCCESS"
    EVENT_LOGIN_FAILED = "LOGIN_FAILED"
    EVENT_LOGIN_BLOCKED_UNVERIFIED = "LOGIN_BLOCKED_UNVERIFIED"
    EVENT_VERIFY_EMAIL_SUCCESS = "VERIFY_EMAIL_SUCCESS"
    EVENT_VERIFY_EMAIL_FAILED = "VERIFY_EMAIL_FAILED"
    EVENT_RESEND_VERIFICATION = "RESEND_VERIFICATION"

    EVENT_CHOICES = [
        (EVENT_LOGIN_SUCCESS, "Login Success"),
        (EVENT_LOGIN_FAILED, "Login Failed"),
        (EVENT_LOGIN_BLOCKED_UNVERIFIED, "Login Blocked (Unverified)"),
        (EVENT_VERIFY_EMAIL_SUCCESS, "Verify Email Success"),
        (EVENT_VERIFY_EMAIL_FAILED, "Verify Email Failed"),
        (EVENT_RESEND_VERIFICATION, "Resend Verification Email"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="auth_events",
    )

    event_type = models.CharField(max_length=50, choices=EVENT_CHOICES)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} @ {self.created_at}"


# =====================================================
# EMAIL VERIFICATION TOKEN
# =====================================================

class EmailVerificationToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="email_verification_tokens",
    )

    token = models.UUIDField(default=uuid.uuid4, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["expires_at"]),
        ]

    @classmethod
    def generate(cls, user):
        return cls.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"VerificationToken for {self.user.email}"


# accounts/models.py

class TeacherProfile(models.Model):
    ROLE_TEACHER = "TEACHER"
    ROLE_ASSISTANT = "ASSISTANT"

    DISPLAY_ROLE_CHOICES = [
        (ROLE_TEACHER, "Teacher"),
        (ROLE_ASSISTANT, "Assistant"),
    ]

    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    ]

    QUALIFICATION_CHOICES = [
        ("high_school", "High School"),
        ("intermediate", "Intermediate"),
        ("bachelors", "Bachelor's Degree"),
        ("masters", "Master's Degree"),
        ("phd", "Ph.D."),
        ("bed", "B.Ed."),
        ("med", "M.Ed."),
        ("diploma", "Diploma"),
        ("other", "Other"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="teacher_profile"
    )

    qualification = models.CharField(max_length=255, blank=True)
    bio = models.TextField(blank=True)

    photo = models.ImageField(
        upload_to="teachers/",
        null=True,
        blank=True
    )

    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True
    )

    is_approved = models.BooleanField(default=False)

    # Form Fillup fields
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    father_name = models.CharField(max_length=150, blank=True)
    father_phone = models.CharField(max_length=15, blank=True)
    mother_name = models.CharField(max_length=150, blank=True)
    mother_phone = models.CharField(max_length=15, blank=True)
    current_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    same_as_current = models.BooleanField(default=False)
    highest_qualification = models.CharField(
        max_length=20, choices=QUALIFICATION_CHOICES, blank=True
    )
    other_qualification = models.CharField(max_length=150, blank=True)
    subject_specialization = models.CharField(max_length=200, blank=True)
    teaching_experience_years = models.PositiveIntegerField(default=0)
    previous_institution = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.same_as_current:
            self.permanent_address = self.current_address
        super().save(*args, **kwargs)

    @property
    def is_complete(self):
        profile = getattr(self.user, "profile", None)
        return bool(
            profile
            and profile.full_name
            and profile.phone
            and self.gender
            and self.date_of_birth
            and self.father_name
            and self.mother_name
            and self.current_address
            and (self.permanent_address or self.same_as_current)
            and self.highest_qualification
            and self.subject_specialization
        )

    def __str__(self):
        return f"TeacherProfile → {self.user.email}"
