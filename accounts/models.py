import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password, check_password
from django.db import models
from django.db.models import Q


# =====================================================
# USER
# =====================================================

# Bump this whenever TermsCondition.jsx's content materially changes, so
# accepted_terms_version on existing accounts stays a true record of what
# they agreed to — a version bump does NOT retroactively re-flag existing
# users as needing to re-accept (no re-consent flow exists yet; that's a
# separate, deliberate scope cut).
CURRENT_TERMS_VERSION = "2026-08-16"


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True)

    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    # Terms-of-Use acceptance, recorded at signup. `accepted_terms_version` is
    # a plain string version tag (see CURRENT_TERMS_VERSION above), not a FK —
    # TermsCondition.jsx is static content with no versioned CMS backing
    # today, unlike the AgreementLetter/AgreementLetterVersion system faculty
    # agreements use. blank/null means an account created before this field
    # existed, or an account created through a path that doesn't collect it.
    accepted_terms_version = models.CharField(max_length=20, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

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

    def default_learner_profile(self):
        """The account's default LearnerProfile, or None.

        Personal/academic data lives on LearnerProfile now; this replaces the
        old one-to-one ``user.profile`` accessor.

        ⚠️ The final `or qs.first()` can return a CHILD profile. That is fine
        for "which profile should this account land on", but it is NOT safe for
        anything that publishes the ACCOUNT HOLDER'S identity — use
        ``self_learner_profile()`` for that. See its docstring.
        """
        qs = self.learner_profiles.filter(is_active=True)
        return (
            qs.filter(is_default=True).first()
            or qs.filter(relationship="SELF").first()
            or qs.first()
        )

    def self_learner_profile(self):
        """The ACCOUNT HOLDER'S own profile — never a dependant's.

        Use this wherever a name or photo is published as *this adult's*
        identity: a Skill Dev expert card, a teacher application, the name on
        a course. `default_learner_profile()` must not be used there, because
        its last resort is `qs.first()` — any profile on the account.

        That was reachable, not theoretical: deleting the SELF profile is
        allowed (accounts/auth_flow.py's ProfileDetailView.delete only blocks
        the last profile and ones holding a live subscription), and when the
        DEFAULT profile is deleted the code promotes the oldest remaining
        active profile with no relationship filter. A parent-expert who
        removed their own profile therefore had a CHILD promoted to default —
        and that child's name and photo became their public marketplace
        identity on a directory anyone can browse.

        Returns None rather than guessing when the account holds no SELF
        profile; callers fall back to the account's own username/email.
        """
        # Honour a prefetch when the caller has one. The public expert
        # directory calls this once per row, so on a 40-expert page the
        # queryset form is 40 extra queries on a page anyone can load without
        # logging in. `.filter()` always hits the DB even when
        # learner_profiles is already prefetched, so branch explicitly.
        if "learner_profiles" in getattr(self, "_prefetched_objects_cache", {}):
            rows = [p for p in self.learner_profiles.all()
                    if p.is_active and p.relationship == "SELF"]
            return (next((p for p in rows if p.is_default), None)
                    or (rows[0] if rows else None))

        qs = self.learner_profiles.filter(is_active=True, relationship="SELF")
        return qs.filter(is_default=True).first() or qs.first()

    def get_active_roles(self):
        return list(
            self.user_roles.filter(is_active=True)
            .values_list("role__name", flat=True)
        )

    def get_permissions(self):
        """Set of permission codenames the user holds through active roles.

        Superusers/staff implicitly hold every permission. The result is
        cached on the instance for the life of the request to avoid N+1s.
        """
        if getattr(self, "_perm_cache", None) is not None:
            return self._perm_cache

        if self.is_superuser or self.is_staff:
            from django.apps import apps
            Permission = apps.get_model("accounts", "Permission")
            perms = set(Permission.objects.values_list("codename", flat=True))
        else:
            perms = set(
                self.user_roles.filter(is_active=True)
                .values_list("role__role_permissions__permission__codename", flat=True)
            )
            perms.discard(None)
        self._perm_cache = perms
        return perms

    def has_permission(self, codename):
        """True if the user holds ``codename`` (staff/superusers hold all)."""
        if self.is_superuser or self.is_staff:
            return True
        return codename in self.get_permissions()


# =====================================================
# PROFILE (Common for all users)
# =====================================================

# =====================================================
# LEARNER PROFILE  (one account -> many learners)
# =====================================================
#
# This replaces the "one User == one learner" assumption. One account
# (User) owns several LearnerProfiles: the account holder plus any
# dependents (children). Each profile carries its own student_id,
# academic info, and an optional switch-PIN. There is no per-profile
# password in the lighter model — the account password authenticates,
# then a profile is selected (PIN-gated for dependents) and rides in
# the JWT as the `active_profile` claim.

class LearnerProfile(models.Model):
    RELATIONSHIP_SELF = "SELF"
    RELATIONSHIP_DEPENDENT = "DEPENDENT"
    RELATIONSHIP_CHOICES = [
        (RELATIONSHIP_SELF, "Account holder"),
        (RELATIONSHIP_DEPENDENT, "Dependent / child"),
    ]

    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
        ("prefer_not_to_say", "Prefer not to say"),
    ]
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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="learner_profiles",
    )

    # Shown in the profile picker ("Profile 1", or the child's name).
    display_name = models.CharField(max_length=100)
    relationship = models.CharField(
        max_length=10, choices=RELATIONSHIP_CHOICES, default=RELATIONSHIP_SELF
    )

    # Hashed switch-PIN. Blank = no PIN required to enter this profile.
    pin = models.CharField(max_length=128, blank=True)

    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # --- Personal Info ---
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    profile_photo = models.ImageField(upload_to="learners/photos/", null=True, blank=True)
    avatar_image = models.ImageField(upload_to="learners/avatar/", null=True, blank=True)
    avatar_emoji = models.CharField(max_length=10, blank=True)

    # One-line self-description shown next to the display name in Settings →
    # Profiles. Previously kept per-device in localStorage by SettingsModal,
    # which meant a parent's edit vanished on any other browser — this is the
    # server-side home for it.
    bio = models.CharField(max_length=280, blank=True)

    student_id = models.CharField(max_length=50, unique=True, null=True, blank=True)

    # --- Address ---
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city_town = models.CharField(max_length=150, blank=True)
    pin_code = models.CharField(max_length=10, blank=True)

    # --- Parent / Guardian (meaningful for DEPENDENT profiles;
    #     for SELF the account holder is the contact) ---
    father_name = models.CharField(max_length=150, blank=True)
    father_phone = models.CharField(max_length=15, blank=True)
    mother_name = models.CharField(max_length=150, blank=True)
    mother_phone = models.CharField(max_length=15, blank=True)
    guardian_name = models.CharField(max_length=150, blank=True)
    guardian_phone = models.CharField(max_length=15, blank=True)
    parent_guardian_email = models.EmailField(blank=True)

    # --- Academic Info ---
    currently_studying = models.CharField(
        max_length=3, choices=CURRENTLY_STUDYING_CHOICES, blank=True
    )
    current_class = models.CharField(max_length=5, choices=CLASS_CHOICES, blank=True)
    stream = models.CharField(max_length=20, choices=STREAM_CHOICES, blank=True)
    board = models.CharField(max_length=20, choices=BOARD_CHOICES, blank=True)
    board_other = models.CharField(max_length=150, blank=True)
    school_name = models.CharField(max_length=250, blank=True)
    academic_year = models.CharField(max_length=20, blank=True)
    highest_education = models.CharField(
        max_length=10, choices=HIGHEST_EDUCATION_CHOICES, blank=True
    )
    reason_not_studying = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "created_at"]
        constraints = [
            # At most one default profile per account.
            models.UniqueConstraint(
                fields=["account"],
                condition=Q(is_default=True),
                name="one_default_learner_per_account",
            )
        ]
        indexes = [
            models.Index(fields=["account", "is_active"]),
        ]

    # --- PIN helpers ---
    def set_pin(self, raw_pin):
        self.pin = make_password(raw_pin) if raw_pin else ""

    def check_pin(self, raw_pin):
        # No PIN configured -> entry is open (used by SELF profiles).
        if not self.pin:
            return True
        return check_password(raw_pin, self.pin)

    def has_pin(self):
        return bool(self.pin)

    # --- Avatar helpers ---
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

    def save(self, *args, **kwargs):
        if self.first_name or self.last_name:
            self.full_name = f"{self.first_name} {self.last_name}".strip()
        if not self.display_name:
            self.display_name = self.full_name or "Learner"
        super().save(*args, **kwargs)

    @property
    def is_complete(self):
        has_personal = bool(
            self.first_name and self.last_name and self.phone and self.date_of_birth
        )
        has_address = bool(self.state and self.district and self.city_town)
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
            and self.account.is_verified
        )

    def __str__(self):
        return f"{self.account.email} · {self.display_name}"


# =====================================================
# ROLE (STRICT)
# =====================================================

class Role(models.Model):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    ADMIN = "ADMIN"
    GUEST = "GUEST"
    MODERATOR = "MODERATOR"

    ROLE_CHOICES = [
        (STUDENT, "Student"),
        (TEACHER, "Teacher"),
        (ADMIN, "Admin"),
        (GUEST, "Guest"),
        (MODERATOR, "Moderator"),
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
        # A user can hold several active roles at once (e.g. an approved
        # TEACHER who also learns). We only keep the "one primary role"
        # invariant for deciding a default landing surface.
        if self.is_primary:
            existing_primary = UserRole.objects.filter(
                user=self.user,
                is_primary=True
            ).exclude(pk=self.pk)

            if existing_primary.exists():
                raise ValidationError("User already has a primary role.")

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
            if tp:
                # Admin approval gates the academy (faculty) track. Flip it
                # from pending → approved, then resync the legacy fields.
                if tp.academy_status == tp.TRACK_PENDING:
                    tp.academy_status = tp.TRACK_APPROVED
                tp.sync_type_from_tracks()
                tp.save(update_fields=["academy_status", "teacher_type", "is_approved"])

    def __str__(self):
        return f"{self.user.email} -> {self.role.name}"


# =====================================================
# RBAC — PERMISSION + ROLE↔PERMISSION MAPPING
# =====================================================

class Permission(models.Model):
    """A granular, code-checkable capability (e.g. ``forum.moderate``).

    Permissions are grouped into Roles via ``RolePermission``; a user is
    granted a permission through any active role that holds it. ``codename``
    is the stable identifier used in code (``user.has_permission(codename)``).
    """

    codename = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    # UI grouping for the admin permission matrix (e.g. "Forum", "Roles").
    category = models.CharField(max_length=60, default="General")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "codename"]
        indexes = [models.Index(fields=["category"])]

    def __str__(self):
        return self.codename


class RolePermission(models.Model):
    """Grants a single Permission to a single Role."""

    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="role_permissions",
    )
    permission = models.ForeignKey(
        Permission,
        on_delete=models.CASCADE,
        related_name="permission_roles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("role", "permission")
        indexes = [models.Index(fields=["role"])]

    def __str__(self):
        return f"{self.role.name} :: {self.permission.codename}"


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
# PASSWORD RESET CODE
# =====================================================
#
# Code-based reset (no emailed links). Flow:
#   request → a 6-digit code is hashed + stored, emailed to the user
#   verify  → the raw code is checked; on success a one-time `ticket`
#             (UUID) is issued so the final step needs no code re-entry
#   confirm → the ticket sets the new password and burns the record
#
# One email == one User, so there is exactly one active code per account.

class PasswordResetCode(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_reset_codes",
    )

    code_hash = models.CharField(max_length=128)
    # Issued only after the code is verified; used by the confirm step.
    ticket = models.UUIDField(null=True, blank=True, unique=True)

    used = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["user", "used"]),
            models.Index(fields=["ticket"]),
            models.Index(fields=["expires_at"]),
        ]

    CODE_TTL = timedelta(minutes=15)
    MAX_ATTEMPTS = 5

    @classmethod
    def issue(cls, user):
        """Invalidate prior codes and create a new one. Returns (obj, raw_code)."""
        import secrets

        cls.objects.filter(user=user, used=False).update(used=True)
        raw_code = f"{secrets.randbelow(1_000_000):06d}"
        obj = cls.objects.create(
            user=user,
            code_hash=make_password(raw_code),
            expires_at=timezone.now() + cls.CODE_TTL,
        )
        return obj, raw_code

    def is_expired(self):
        return timezone.now() > self.expires_at

    def check_code(self, raw_code):
        return check_password(raw_code, self.code_hash)

    def __str__(self):
        return f"ResetCode for {self.user.email}"


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

    # Course-application taxonomy (grouped ranges + UG/PG). Stored in choice-less
    # JSONFields (TeacherProfile.classes/.streams, TeacherCourseApplication
    # .classes/.streams) so changing these needs NO migration. They feed the
    # label maps in the public-profile view and the form-fillup validators. The
    # student LearnerProfile keeps its own 8-12 CLASS/STREAM choices — do NOT merge.
    CLASS_CHOICES = [
        ("1_5", "Class 1–5"),
        ("6_8", "Class 6–8"),
        ("9_10", "Class 9–10"),
        ("11_12", "Class 11–12"),
        ("ug", "Undergraduate"),
        ("pg", "Postgraduate"),
    ]

    STREAM_CHOICES = [
        ("science", "Science"),
        ("commerce", "Commerce"),
        ("arts", "Arts / Humanities"),
        ("vocational", "Vocational"),
        ("general", "General"),
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

    # Which track the teacher applied through. A teacher may ultimately do
    # both (course_applications AND skill_applications already coexist on
    # this model); this records the primary track chosen at signup and
    # which dashboard they land on. "BOTH" is allowed once approved for each.
    TYPE_GUEST = "GUEST"      # specialized-skills "guest expert"
    TYPE_FACULTY = "FACULTY"  # academic class 8-12 faculty
    TYPE_BOTH = "BOTH"
    TEACHER_TYPE_CHOICES = [
        (TYPE_GUEST, "Guest expert (skills)"),
        (TYPE_FACULTY, "Faculty (academic)"),
        (TYPE_BOTH, "Both"),
    ]

    # Per-track lifecycle. A teacher is "assigned" to a track by an admin
    # (academy/faculty) or auto-listed (skill/guest). The dashboard switch in
    # both the teacher and student apps reads these directly:
    #   locked    → not applied for; the switch tile shows a padlock + "Apply"
    #   pending   → applied, waiting on admin review; tile shows "In review"
    #   approved  → live; tile is selectable and routes to that dashboard
    # "academy" maps to the FACULTY track, "skill" maps to the GUEST track.
    TRACK_LOCKED = "locked"
    TRACK_PENDING = "pending"
    TRACK_APPROVED = "approved"
    TRACK_REJECTED = "rejected"
    TRACK_STATUS_CHOICES = [
        (TRACK_LOCKED, "Locked"),
        (TRACK_PENDING, "Pending review"),
        (TRACK_APPROVED, "Approved"),
        (TRACK_REJECTED, "Rejected"),
    ]

    # The two switchable tracks, by the public name used across the apps.
    TRACK_ACADEMY = "academy"
    TRACK_SKILL = "skill"

    # Tier assigned by the screening panel; drives the rate band.
    TIER_STANDARD = "standard"
    TIER_SENIOR = "senior"
    TIER_EXPERT = "expert"
    TIER_CHOICES = [
        (TIER_STANDARD, "Standard"),
        (TIER_SENIOR, "Senior"),
        (TIER_EXPERT, "Expert"),
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
    # SMS-reachable mobile for booking confirmations/cancellations and
    # session reminders (notifications.phone.phone_for_user). Optional —
    # faculty signup doesn't collect it yet, so SMS to teachers is
    # gracefully skipped (SmsLog status "skipped") until the profile UI
    # asks for it.
    phone = models.CharField(max_length=20, blank=True, default="")
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    is_approved = models.BooleanField(default=False)

    # --- Track + tier ---
    teacher_type = models.CharField(
        max_length=10, choices=TEACHER_TYPE_CHOICES, default=TYPE_FACULTY
    )
    tier = models.CharField(max_length=10, choices=TIER_CHOICES, blank=True)

    # Per-track status (see TRACK_* above). teacher_type stays in sync via
    # sync_type_from_tracks() for backward compatibility with older code.
    academy_status = models.CharField(
        max_length=10, choices=TRACK_STATUS_CHOICES, default=TRACK_LOCKED
    )
    skill_status = models.CharField(
        max_length=10, choices=TRACK_STATUS_CHOICES, default=TRACK_LOCKED
    )
    # When the academy (faculty) application is rejected, the admin's reason is
    # stored here so the teacher can see why and re-apply.
    academy_rejection_reason = models.TextField(blank=True)
    academy_rejected_at = models.DateTimeField(null=True, blank=True)

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

    # --- Signed faculty agreement (collected from the dashboard /form-fillup
    #     after email verification; see FacultySignup flow + 0016 migration) ---
    signed_agreement = models.FileField(
        upload_to="teachers/agreements/", null=True, blank=True
    )
    # The exact agreement version this faculty member signed (bound at sign
    # time so later edits to the letter never change what they agreed to).
    signed_agreement_version = models.ForeignKey(
        "accounts.AgreementLetterVersion",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="signed_by",
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


    # ── Track helpers ─────────────────────────────────────────────────────
    def track_status(self, track):
        """Status for 'academy' or 'skill'."""
        if track == self.TRACK_ACADEMY:
            return self.academy_status
        if track == self.TRACK_SKILL:
            return self.skill_status
        return self.TRACK_LOCKED

    def set_track_status(self, track, status):
        if track == self.TRACK_ACADEMY:
            self.academy_status = status
        elif track == self.TRACK_SKILL:
            self.skill_status = status

    def approved_tracks(self):
        out = []
        if self.academy_status == self.TRACK_APPROVED:
            out.append(self.TRACK_ACADEMY)
        if self.skill_status == self.TRACK_APPROVED:
            out.append(self.TRACK_SKILL)
        return out

    def pending_tracks(self):
        out = []
        if self.academy_status == self.TRACK_PENDING:
            out.append(self.TRACK_ACADEMY)
        if self.skill_status == self.TRACK_PENDING:
            out.append(self.TRACK_SKILL)
        return out

    @property
    def is_academy_faculty(self):
        """True iff a human admin reviewed this teacher for school teaching.

        The single source of truth for "may act as Academy faculty". Lives on
        the model so `accounts` and `courses` can share one definition without
        importing each other (courses.admin_academy_views.is_academy_faculty
        delegates here).

        Deliberately NOT `is_approved`, and NOT `teacher_type`:
          • `is_approved` is `bool(approved_tracks())`, and the Skill track
            AUTO-approves at signup with no review (signup_serializer's
            `_initial_status_for`) — so every self-registered guest expert has
            is_approved=True despite never being vetted.
          • `teacher_type` counts a track as "on" while merely PENDING (see
            sync_type_from_tracks), so it is not proof of approval either.
        Holding BOTH tracks approved passes — genuine faculty who also sell
        skill sessions.
        """
        return self.academy_status == self.TRACK_APPROVED

    @staticmethod
    def track_for_type(teacher_type):
        """Map a signup teacher_type (GUEST/FACULTY) to a track name."""
        return (
            TeacherProfile.TRACK_SKILL
            if teacher_type == TeacherProfile.TYPE_GUEST
            else TeacherProfile.TRACK_ACADEMY
        )

    def sync_type_from_tracks(self):
        """Keep the legacy teacher_type + is_approved in step with the
        per-track statuses so existing dashboards/admin keep working.

        A track counts toward teacher_type once it is applied for (pending or
        approved). is_approved is True whenever ANY track is live, which is
        what the legacy gates ("teacher account active") really meant."""
        academy_on = self.academy_status in (self.TRACK_PENDING, self.TRACK_APPROVED)
        skill_on = self.skill_status in (self.TRACK_PENDING, self.TRACK_APPROVED)
        if academy_on and skill_on:
            self.teacher_type = self.TYPE_BOTH
        elif skill_on:
            self.teacher_type = self.TYPE_GUEST
        elif academy_on:
            self.teacher_type = self.TYPE_FACULTY
        self.is_approved = bool(self.approved_tracks())

    # ── Track-add policy (the asymmetric Faculty / Guest rule) ─────────────
    #
    # Business rule (single source of truth — encoded purely from status so
    # the signup, add-track, switcher and settings paths can never drift):
    #   • A teacher who FIRST became Faculty (academy) can NOT later add the
    #     Skill / Guest-expert track — faculty stay faculty-only, one dashboard.
    #   • A teacher who FIRST became a Guest expert (skill) CAN later add the
    #     Faculty (academy) track — they then get the two-dashboard switcher.
    #
    #   ⇒ the Skill track may be added only when Academy was never taken
    #     (academy_status == locked) AND Skill isn't already held.
    #   ⇒ the Academy track may be added whenever it isn't already held;
    #     holding the Skill track does NOT block it.
    def holds_track(self, track):
        """True if the track is already live or in review (i.e. 'held')."""
        return self.track_status(track) in (self.TRACK_PENDING, self.TRACK_APPROVED)

    def can_apply_track(self, track):
        """Whether this profile is allowed to ADD `track` right now."""
        if track == self.TRACK_ACADEMY:
            # Faculty can always be added if not already held.
            return not self.holds_track(self.TRACK_ACADEMY)
        if track == self.TRACK_SKILL:
            # Skill / Guest only if Academy was never held and Skill isn't held.
            return (
                not self.holds_track(self.TRACK_ACADEMY)
                and not self.holds_track(self.TRACK_SKILL)
            )
        return False

    def track_add_block_reason(self, track):
        """Human-readable reason `track` can't be added, or '' if it can."""
        if self.can_apply_track(track):
            return ""
        if track == self.TRACK_SKILL and self.holds_track(self.TRACK_ACADEMY):
            return (
                "Faculty accounts can't add the Skill Dev (Guest expert) track. "
                "Guest experts can add Faculty, but not the other way around."
            )
        if self.holds_track(track):
            nice = ("Academy (Faculty)" if track == self.TRACK_ACADEMY
                    else "Skill (Guest expert)")
            return f"You're already set up for {nice} on this account. Log in instead."
        return "That track can't be added to this account."

    def save(self, *args, **kwargs):
        if self.same_as_current:
            self.permanent_address = self.current_address
        super().save(*args, **kwargs)

    @property
    def is_complete(self):
        """Check if teacher profile form is complete."""
        profile = self.user.default_learner_profile()
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



# ═══════════════════════════════════════════════════════════════════════════
# Agreement letters (admin-editable, immutable version history)
# ═══════════════════════════════════════════════════════════════════════════
class AgreementLetter(models.Model):
    """A named legal document (e.g. the Faculty Agreement).

    The document itself is a stable pointer; its text lives in immutable
    AgreementLetterVersion rows. Editing never mutates a version — each Save
    creates a NEW version and repoints ``current_version``. Faculty are bound
    to the exact version they signed (TeacherProfile.signed_agreement_version),
    so later edits never change what someone already agreed to.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=50, unique=True)     # e.g. "faculty"
    title = models.CharField(max_length=200)
    current_version = models.ForeignKey(
        "accounts.AgreementLetterVersion",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AgreementLetter<{self.key}>"


class AgreementLetterVersion(models.Model):
    """An immutable snapshot of an agreement's text at a point in time."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    letter = models.ForeignKey(
        AgreementLetter, on_delete=models.CASCADE, related_name="versions"
    )
    version_number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    body = models.TextField()
    change_note = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        unique_together = ("letter", "version_number")
        indexes = [models.Index(fields=["letter", "version_number"])]

    def __str__(self):
        return f"{self.letter.key} v{self.version_number}"


# ===========================================================================
# IDENTITY REGISTRY  (M1 — Phase 3 architecture §6)
# ===========================================================================
#
# One row per chat-addressable identity on the platform. Formalizes a
# convention that already existed informally: chat.Participant.identity_key()
# has always produced strings like "L:<uuid>" / "T:<uuid>" from
# (kind, learner_profile_id / teacher_profile_id). This table makes that
# convention a real, queryable registry instead of a string format repeated
# across chat/models.py, chat/services.py, and chat/consumers.py.
#
# WHY A SOFT REFERENCE, NOT A DIRECT FK PER KIND:
#   Adding a new identity kind (Counsellor, Recruiter, ...) must cost "one new
#   letter in KIND_CHOICES + one profile model in that vertical's app" — never
#   a schema change here. So `profile_id` is a plain string, resolved by kind
#   at read time (see resolve_profile()), the same soft-reference pattern
#   chat.Conversation already uses for course_id.
#
#   IMPORTANT — profile_id is a CharField, not a UUIDField: LearnerProfile's
#   pk IS a UUID, but TeacherProfile's is a plain BigAutoField integer (found
#   via testing, not assumption — the two tables were never actually
#   symmetric here). A UUIDField would silently coerce an integer teacher id
#   into a fake UUID (e.g. teacher id 1 -> "00000000-...-0001") instead of
#   erroring, which is exactly the kind of bug that stays invisible until
#   someone compares it against Participant.identity_key()'s plain "T:1". A
#   CharField stores whatever str(pk) actually is for either kind, correctly.
#
# WHY account IS NULLABLE:
#   KIND_SYSTEM identities (an announcement bot, "Shiksha Support") are not
#   tied to any one login.
#
# MIGRATION STATUS: additive. chat.Participant / chat.Block gain a nullable
# `identity` FK alongside their existing polymorphic columns; both are
# dual-written until Phase M3 removes the old columns. Nothing that reads
# the old columns today changes behaviour.
class Identity(models.Model):
    KIND_LEARNER   = "L"
    KIND_TEACHER   = "T"
    KIND_COUNSELOR = "C"   # reserved for the Counselling vertical (Phase 3 §21)
    KIND_RECRUITER = "R"   # reserved for the Placement vertical (Phase 3 §22)
    KIND_SYSTEM    = "S"   # announcement / support bot senders
    KIND_CHOICES = [
        (KIND_LEARNER, "Learner profile"),
        (KIND_TEACHER, "Teacher identity"),
        (KIND_COUNSELOR, "Counsellor identity"),
        (KIND_RECRUITER, "Recruiter identity"),
        (KIND_SYSTEM, "System / bot identity"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=1, choices=KIND_CHOICES, db_index=True)

    # Soft pointer to the concrete profile row, stored as str(pk) — see the
    # CharField-not-UUIDField note in the module docstring above. Null only
    # for a KIND_SYSTEM identity with no backing row.
    profile_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # Denormalized so inbox/notification rendering never joins out to the
    # concrete profile table just to show a name and avatar. Kept fresh by
    # accounts/signals.py on every profile save.
    display_name = models.CharField(max_length=150, blank=True)
    avatar_url = models.CharField(max_length=500, blank=True)

    # Null for KIND_SYSTEM only. SET_NULL (not CASCADE): deleting a User must
    # orphan this row for audit purposes, not silently delete a chat identity
    # out from under existing conversations — those are cleaned up, if ever,
    # by an explicit deactivation flow, not a cascade.
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="identities",
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "profile_id"],
                name="uniq_identity_per_profile",
            ),
        ]
        indexes = [
            models.Index(fields=["account"], name="idx_identity_account"),
        ]

    @property
    def key(self):
        """Matches chat.Participant.identity_key()'s existing string format
        exactly — "L:<uuid>" / "T:<uuid>" — so this table can be looked up
        by a key string already flowing through the rest of the system
        without introducing a second format."""
        return f"{self.kind}:{self.profile_id}"

    @classmethod
    def kind_for_participant_kind(cls, participant_kind):
        """Map chat.Participant's "LEARNER"/"TEACHER" strings to this
        table's single-letter kind. The single-letter form already exists
        implicitly (it's the first character of identity_key()); this just
        names the mapping once instead of leaving it to string-slicing at
        every call site."""
        return {"LEARNER": cls.KIND_LEARNER, "TEACHER": cls.KIND_TEACHER}[participant_kind]

    def resolve_profile(self):
        """Fetch the concrete LearnerProfile / TeacherProfile row this
        identity points to, or None. Mirrors chat.services.resolve_identity()
        — kept here too since not every consumer of this table wants to
        import from the chat app."""
        if self.kind == self.KIND_LEARNER:
            return LearnerProfile.objects.filter(id=self.profile_id).first()
        if self.kind == self.KIND_TEACHER:
            return TeacherProfile.objects.filter(id=self.profile_id).first()
        return None

    def __str__(self):
        return f"Identity<{self.key}> {self.display_name}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Settings-surface models (Sessions & devices, Learning goals, Account
# deletion) live in a side module to keep this file navigable — same pattern as
# skills/payment_models.py. Imported here so Django registers them under the
# `accounts` app label.
# ─────────────────────────────────────────────────────────────────────────────
from .settings_models import (  # noqa: F401, E402
    AccountDeletionRequest,
    LearningGoal,
    TourState,
    UserSession,
)
