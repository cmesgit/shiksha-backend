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

    # Safe role checker (ONLY use Role constants)
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
# PROFILE (Common for all users)
# =====================================================

class Profile(models.Model):
    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
        ("prefer_not_to_say", "Prefer not to say"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    # --- Personal Info ---
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    profile_photo = models.ImageField(upload_to="profiles/photos/", null=True, blank=True)

    # --- Legacy fields (kept for backward compat, will be removed later) ---
    full_name = models.CharField(max_length=255, null=True, blank=True)

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

    # --- Address (structured) ---
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city_town = models.CharField(max_length=150, blank=True)
    pin_code = models.CharField(max_length=10, blank=True)

    # --- Legacy address fields (kept for backward compat) ---
    current_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    same_as_current = models.BooleanField(default=False)

    # --- Student: Parent/Guardian Info ---
    father_name = models.CharField(max_length=150, blank=True)
    father_phone = models.CharField(max_length=15, blank=True)
    mother_name = models.CharField(max_length=150, blank=True)
    mother_phone = models.CharField(max_length=15, blank=True)
    guardian_name = models.CharField(max_length=150, blank=True)
    guardian_phone = models.CharField(max_length=15, blank=True)
    parent_guardian_email = models.EmailField(blank=True)

    # --- Legacy parent fields ---
    guardian = models.CharField(max_length=150, blank=True)

    # --- Student: Academic Info ---
    CURRENTLY_STUDYING_CHOICES = [
        ("yes", "Yes"),
        ("no", "No"),
    ]

    CLASS_CHOICES = [
        ("8", "Class 8"),
        ("9", "Class 9"),
        ("10", "Class 10"),
        ("11", "Class 11"),
        ("12", "Class 12"),
    ]

    STREAM_CHOICES = [
        ("science", "Science"),
        ("commerce", "Commerce"),
        ("arts", "Arts"),
    ]

    BOARD_CHOICES = [
        ("cbse", "CBSE"),
        ("icse", "ICSE"),
        ("mbse", "Mizoram Board of School Education"),
        ("nios", "NIOS"),
        ("other", "Other State Board"),
    ]

    HIGHEST_EDUCATION_CHOICES = [
        ("below_8", "Below Class 8"),
        ("8", "Class 8"),
        ("9", "Class 9"),
        ("10", "Class 10"),
        ("11", "Class 11"),
        ("12", "Class 12"),
    ]

    currently_studying = models.CharField(
        max_length=3, choices=CURRENTLY_STUDYING_CHOICES, blank=True
    )

    # If currently studying = yes
    current_class = models.CharField(max_length=5, choices=CLASS_CHOICES, blank=True)
    stream = models.CharField(max_length=20, choices=STREAM_CHOICES, blank=True)
    board = models.CharField(max_length=20, choices=BOARD_CHOICES, blank=True)
    board_other = models.CharField(max_length=150, blank=True)
    school_name = models.CharField(max_length=250, blank=True)
    academic_year = models.CharField(max_length=20, blank=True)

    # If currently studying = no
    highest_education = models.CharField(
        max_length=10, choices=HIGHEST_EDUCATION_CHOICES, blank=True
    )
    reason_not_studying = models.CharField(max_length=200, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Keep legacy full_name in sync
        if self.first_name or self.last_name:
            self.full_name = f"{self.first_name} {self.last_name}".strip()
        # Legacy address sync
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
        """Check if student profile form is complete."""
        has_personal = bool(
            self.first_name
            and self.last_name
            and self.phone
            and self.date_of_birth
        )
        has_address = bool(self.state and self.district and self.city_town)

        # At least one complete parent/guardian contact (name + phone)
        has_parent_contact = (
            (bool(self.father_name) and bool(self.father_phone))
            or (bool(self.mother_name) and bool(self.mother_phone))
            or (bool(self.guardian_name) and bool(self.guardian_phone))
        )

        has_academic = bool(self.currently_studying)

        return bool(
            has_personal
            and has_address
            and has_parent_contact
            and has_academic
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

    # Only ONE primary role per user
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

        if self.role.name == "TEACHER":
            tp = getattr(self.user, "teacher_profile", None)
            if tp and not tp.is_approved:
                tp.is_approved = True
                tp.save(update_fields=["is_approved"])

    def __str__(self):
        return f"{self.user.email} -> {self.role.name}"


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


# =====================================================
# TEACHER PROFILE
# =====================================================

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
        ("prefer_not_to_say", "Prefer not to say"),
    ]

    HIGHEST_DEGREE_CHOICES = [
        ("10th_pass", "10th Pass"),
        ("12th_pass", "12th Pass"),
        ("diploma", "Diploma"),
        ("bachelors", "Bachelor's Degree"),
        ("masters", "Master's Degree"),
        ("phd", "Ph.D."),
        ("other", "Other"),
    ]

    EXPERIENCE_CHOICES = [
        ("0", "New Teacher (0 years)"),
        ("lt1", "Less than 1 year"),
        ("1_3", "1-3 years"),
        ("3_5", "3-5 years"),
        ("5_10", "5-10 years"),
        ("10plus", "10+ years"),
    ]

    EMPLOYMENT_STATUS_CHOICES = [
        ("fulltime", "Full-time teacher at school"),
        ("parttime", "Part-time teacher"),
        ("private_tutor", "Private tutor"),
        ("unemployed", "Unemployed/Looking for opportunities"),
        ("retired", "Retired teacher"),
    ]

    GOVT_ID_TYPE_CHOICES = [
        ("aadhaar", "Aadhaar Card"),
        ("pan", "PAN Card"),
        ("voter_id", "Voter ID"),
        ("driving_license", "Driving License"),
    ]

    BOARD_CHOICES = [
        ("cbse", "CBSE"),
        ("icse", "ICSE"),
        ("mbse", "Mizoram Board"),
        ("nios", "NIOS"),
    ]

    CLASS_CHOICES = [
        ("8", "Class 8"),
        ("9", "Class 9"),
        ("10", "Class 10"),
        ("11", "Class 11"),
        ("12", "Class 12"),
    ]

    STREAM_CHOICES = [
        ("science", "Science"),
        ("commerce", "Commerce"),
        ("arts", "Arts"),
    ]

    SUBJECT_CHOICES = [
        ("mathematics", "Mathematics"),
        ("physics", "Physics"),
        ("chemistry", "Chemistry"),
        ("biology", "Biology"),
        ("english", "English"),
        ("hindi", "Hindi"),
        ("social_science", "Social Science"),
        ("history", "History"),
        ("geography", "Geography"),
        ("economics", "Economics"),
        ("computer_science", "Computer Science"),
        ("accountancy", "Accountancy"),
        ("business_studies", "Business Studies"),
        ("political_science", "Political Science"),
        ("other", "Other"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="teacher_profile"
    )

    # --- Legacy display fields (kept for teacher listing cards) ---
    qualification = models.CharField(max_length=255, blank=True)
    bio = models.TextField(blank=True)
    photo = models.ImageField(upload_to="teachers/", null=True, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    is_approved = models.BooleanField(default=False)

    # --- Section 1: Educational Qualifications ---
    highest_degree = models.CharField(
        max_length=20, choices=HIGHEST_DEGREE_CHOICES, blank=True
    )
    field_of_study = models.CharField(max_length=200, blank=True)
    year_of_completion = models.PositiveIntegerField(null=True, blank=True)
    teaching_certifications = models.JSONField(default=list, blank=True)
    qualification_certificate = models.FileField(
        upload_to="teachers/certificates/", null=True, blank=True
    )

    # --- Section 2: Teaching Experience ---
    experience_range = models.CharField(
        max_length=10, choices=EXPERIENCE_CHOICES, blank=True
    )
    employment_status = models.CharField(
        max_length=20, choices=EMPLOYMENT_STATUS_CHOICES, blank=True
    )
    currently_employed = models.BooleanField(default=False)
    current_institution = models.CharField(max_length=250, blank=True)
    current_position = models.CharField(max_length=150, blank=True)

    # --- Section 3: Verification Documents ---
    govt_id_type = models.CharField(
        max_length=20, choices=GOVT_ID_TYPE_CHOICES, blank=True
    )
    id_number = models.CharField(max_length=50, blank=True)
    id_proof_front = models.FileField(
        upload_to="teachers/id_proofs/", null=True, blank=True
    )
    id_proof_back = models.FileField(
        upload_to="teachers/id_proofs/", null=True, blank=True
    )

    # --- Course Application fields ---
    subject = models.CharField(max_length=50, choices=SUBJECT_CHOICES, blank=True)
    boards = models.JSONField(default=list, blank=True)
    classes = models.JSONField(default=list, blank=True)
    streams = models.JSONField(default=list, blank=True)

    # --- Skill Application fields ---
    skill_name = models.CharField(max_length=200, blank=True)
    skill_description = models.CharField(max_length=500, blank=True)
    skill_related_subject = models.CharField(max_length=50, choices=SUBJECT_CHOICES, blank=True)
    skill_supporting_image = models.ImageField(
        upload_to="teachers/skills/images/", null=True, blank=True
    )
    skill_supporting_video = models.FileField(
        upload_to="teachers/skills/videos/", null=True, blank=True
    )

    # --- Legacy form fillup fields (kept for backward compat) ---
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    father_name = models.CharField(max_length=150, blank=True)
    father_phone = models.CharField(max_length=15, blank=True)
    mother_name = models.CharField(max_length=150, blank=True)
    mother_phone = models.CharField(max_length=15, blank=True)
    current_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    same_as_current = models.BooleanField(default=False)
    highest_qualification = models.CharField(
        max_length=20,
        choices=[
            ("high_school", "High School"),
            ("intermediate", "Intermediate"),
            ("bachelors", "Bachelor's Degree"),
            ("masters", "Master's Degree"),
            ("phd", "Ph.D."),
            ("bed", "B.Ed."),
            ("med", "M.Ed."),
            ("diploma", "Diploma"),
            ("other", "Other"),
        ],
        blank=True
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
        """Check if teacher profile form is complete."""
        profile = getattr(self.user, "profile", None)
        has_personal = bool(
            profile
            and profile.first_name
            and profile.last_name
            and profile.phone
            and profile.date_of_birth
        )
        has_address = bool(
            profile
            and profile.state
            and profile.district
            and profile.city_town
        )
        has_qualifications = bool(
            self.highest_degree
            and self.field_of_study
            and self.year_of_completion
        )
        has_experience = bool(
            self.experience_range
            and self.employment_status
        )
        has_verification = bool(
            self.govt_id_type
            and self.id_number
            and self.id_proof_front
        )
        has_applications = (
            self.course_applications.exists()
            or self.skill_applications.exists()
        )

        return bool(
            has_personal
            and has_address
            and has_qualifications
            and has_experience
            and has_verification
            and has_applications
        )


    def __str__(self):
        return f"TeacherProfile -> {self.user.email}"

class TeacherCourseApplication(models.Model):
    teacher_profile = models.ForeignKey(
        TeacherProfile, on_delete=models.CASCADE, related_name="course_applications"
    )
    subject = models.CharField(max_length=50, choices=TeacherProfile.SUBJECT_CHOICES)
    boards = models.JSONField(default=list)
    classes = models.JSONField(default=list)
    streams = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.teacher_profile.user.email} - {self.get_subject_display()}"


class TeacherSkillApplication(models.Model):
    teacher_profile = models.ForeignKey(
        TeacherProfile, on_delete=models.CASCADE, related_name="skill_applications"
    )
    skill_name = models.CharField(max_length=200)
    skill_description = models.CharField(max_length=500)
    skill_related_subject = models.CharField(
        max_length=50, choices=TeacherProfile.SUBJECT_CHOICES
    )
    supporting_file = models.FileField(
        upload_to="teachers/skills/files/", null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.teacher_profile.user.email} - {self.skill_name}"

