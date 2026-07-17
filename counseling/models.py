# PLACEMENT: backend/backend/counseling/models.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/models.py
#
# Career-counseling app built on the EXISTING account system — no second
# login, no parallel profile stack:
#
#   • A counselor is another role on the same account (Role "COUNSELOR",
#     seeded by migration 0002), exactly like teacher/learner today.
#   • The student in every flow is a LearnerProfile — which means a parent
#     books for a child by using the dependent profile. The counseling
#     doc's "Parent Accounts" phase-2 feature is free.
#   • CounselingIntake only adds what LearnerProfile doesn't already have
#     (career interests, industry, goals, skills). Class/stream/board/
#     gender/age come from the LearnerProfile itself.
#   • Matching is rule-based (services.match_counselors) per the MVP spec:
#     specialization ∩ interests, stream affinity, language, then rating.
#   • Notifications ride the site-wide notifications app (verbs
#     "counseling.*"), email included for bookings and reports.
#   • Video sessions are external links (Meet/Zoom/Jitsi per the spec) on
#     Appointment.meeting_link; in-app counseling chat is a follow-up.

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


# =====================================================
# Specializations (matching vocabulary)
# =====================================================

class Specialization(models.Model):
    """What counselors offer AND what students pick as career interests —
    one shared vocabulary so matching is a simple set intersection.
    Seeded by migration 0002; admins can add more in Django admin."""

    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# =====================================================
# Counselor profile (mirrors the TeacherProfile pattern)
# =====================================================

class CounselorProfile(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    EXPERIENCE_CHOICES = [
        ("lt1", "Less than 1 year"),
        ("1_3", "1-3 years"),
        ("3_5", "3-5 years"),
        ("5_10", "5-10 years"),
        ("10plus", "10+ years"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="counselor_profile",
    )

    display_name = models.CharField(max_length=120)
    # SMS-reachable mobile for booking/cancellation SMS + reminders
    # (notifications.phone.phone_for_user). Optional — blank until the
    # counselor application/profile form collects it; SMS is skipped
    # (SmsLog "skipped") when empty.
    phone = models.CharField(max_length=20, blank=True, default="")
    photo = models.ImageField(upload_to="counselors/photos/", null=True, blank=True)
    bio = models.TextField(blank=True, default="")
    qualifications = models.TextField(blank=True, default="")
    certifications = models.TextField(blank=True, default="")
    approach = models.TextField(
        blank=True, default="",
        help_text="Counseling approach shown on the public profile.",
    )
    years_experience = models.CharField(
        max_length=10, choices=EXPERIENCE_CHOICES, blank=True, default=""
    )
    # Comma-separated language names ("English, Hindi, Mizo") — same
    # convention as elsewhere in the project; matching splits on commas.
    languages = models.CharField(max_length=200, blank=True, default="")
    specializations = models.ManyToManyField(
        Specialization, blank=True, related_name="counselors"
    )

    session_duration_minutes = models.PositiveIntegerField(default=45)

    # Rating denormalised for the directory; reviews are a later phase, so
    # these are admin/seed-set for now and safe to leave at 0.
    avg_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    rating_count = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    is_listed = models.BooleanField(
        default=True,
        help_text="Approved counselors can be temporarily hidden from the directory.",
    )
    review_note = models.CharField(max_length=300, blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="reviewed_counselor_profiles",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.display_name} ({self.status})"

    @property
    def is_bookable(self):
        return self.status == self.STATUS_APPROVED and self.is_listed

    def language_list(self):
        return [x.strip() for x in (self.languages or "").split(",") if x.strip()]


class AvailabilitySlot(models.Model):
    """Weekly recurring availability window (counselor's local schedule).
    Concrete bookable datetimes are materialised by services.bookable_slots
    for the next N days, minus already-booked appointments."""

    WEEKDAYS = [
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    ]

    counselor = models.ForeignKey(
        CounselorProfile, on_delete=models.CASCADE, related_name="availability"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                name="availability_start_before_end",
                condition=models.Q(start_time__lt=models.F("end_time")),
            ),
        ]

    def __str__(self):
        return f"{self.counselor.display_name}: {self.get_weekday_display()} {self.start_time}–{self.end_time}"


# =====================================================
# Student intake (only the fields LearnerProfile lacks)
# =====================================================

class CounselingIntake(models.Model):
    """Career-side onboarding for ONE learner profile. Class, stream,
    board, gender, DOB, location already live on the LearnerProfile —
    do not duplicate them here."""

    WORK_ENV_CHOICES = [
        ("office", "Office / corporate"),
        ("field", "Field work / outdoors"),
        ("remote", "Remote / online"),
        ("creative", "Creative studio"),
        ("research", "Research / academia"),
        ("public", "Public service / government"),
        ("entrepreneur", "Own business / startup"),
        ("mixed", "Mixed / not sure yet"),
    ]

    learner_profile = models.OneToOneField(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="counseling_intake",
    )

    career_interests = models.ManyToManyField(
        Specialization, blank=True, related_name="interested_learners"
    )
    preferred_industry = models.CharField(max_length=150, blank=True, default="")
    work_environment = models.CharField(
        max_length=20, choices=WORK_ENV_CHOICES, blank=True, default=""
    )
    long_term_goals = models.TextField(blank=True, default="")
    short_term_goals = models.TextField(blank=True, default="")
    # Comma-separated from the spec's fixed list (Communication, Leadership,
    # Programming, Creativity, Design, Mathematics, Writing, Public
    # Speaking, Problem Solving) — free additions allowed.
    skills = models.CharField(max_length=400, blank=True, default="")
    languages = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Languages the student is comfortable being counseled in.",
    )
    favorite_subjects = models.CharField(max_length=300, blank=True, default="")

    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Intake: {self.learner_profile.display_name}"

    @property
    def is_complete(self):
        return self.completed_at is not None

    def skill_list(self):
        return [x.strip() for x in (self.skills or "").split(",") if x.strip()]

    def language_list(self):
        return [x.strip() for x in (self.languages or "").split(",") if x.strip()]


# =====================================================
# Appointments
# =====================================================

class Appointment(models.Model):
    STATUS_CONFIRMED = "confirmed"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_NO_SHOW = "no_show"
    STATUS_CHOICES = [
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_NO_SHOW, "No-show"),
    ]

    # WHO the session is for (can be a dependent) and WHO booked it
    # (the account holder — a parent booking for a child).
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="counseling_appointments",
    )
    booked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="booked_counseling_appointments",
    )
    counselor = models.ForeignKey(
        CounselorProfile, on_delete=models.CASCADE, related_name="appointments"
    )

    scheduled_at = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveIntegerField(default=45)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_CONFIRMED, db_index=True
    )
    # External video link per the spec (Meet / Zoom / Jitsi). Counselor
    # sets or updates it; students see it on confirmed appointments.
    meeting_link = models.URLField(blank=True, default="")

    student_note = models.CharField(
        max_length=500, blank=True, default="",
        help_text="What the student wants to discuss (shown to the counselor).",
    )
    cancel_reason = models.CharField(max_length=300, blank=True, default="")
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cancelled_counseling_appointments",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["counselor", "scheduled_at"]),
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self):
        return f"{self.learner_profile.display_name} × {self.counselor.display_name} @ {self.scheduled_at:%d %b %Y %H:%M}"

    @property
    def end_at(self):
        return self.scheduled_at + timedelta(minutes=self.duration_minutes)

    @property
    def is_upcoming(self):
        return self.status == self.STATUS_CONFIRMED and self.scheduled_at >= timezone.now()


# =====================================================
# Career assessment
# =====================================================

class AssessmentTemplate(models.Model):
    """The structured career assessment. `sections` is an ordered list of
    {key, title, questions: [{key, label, type: text|textarea|choices|multi,
    options?}]} — migration 0002 seeds the default template from the spec
    (Personal Interests · Academic Background · Skills · Career Aspirations
    · Strengths · Challenges). Editable in Django admin without deploys."""

    name = models.CharField(max_length=120, unique=True)
    sections = models.JSONField(default=list)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class AssessmentResponse(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_SUBMITTED = "submitted"
    STATUS_CHOICES = [(STATUS_DRAFT, "Draft"), (STATUS_SUBMITTED, "Submitted")]

    appointment = models.OneToOneField(
        Appointment, on_delete=models.CASCADE, related_name="assessment"
    )
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="counseling_assessments",
    )
    template = models.ForeignKey(AssessmentTemplate, on_delete=models.PROTECT)
    # {question_key: answer} — flat map keyed by the template's question keys.
    answers = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Assessment for appointment {self.appointment_id} ({self.status})"


# =====================================================
# Session notes (counselor-private) & reports (student-visible)
# =====================================================

class SessionNote(models.Model):
    """Private working notes. NEVER exposed to students — only the owning
    counselor (and Django admin) can read them."""

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="notes"
    )
    counselor = models.ForeignKey(
        CounselorProfile, on_delete=models.CASCADE, related_name="session_notes"
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Note on appointment {self.appointment_id}"


class SessionReport(models.Model):
    """The deliverable the student receives after a session. Draft until
    published; publishing notifies the student (bell + email)."""

    appointment = models.OneToOneField(
        Appointment, on_delete=models.CASCADE, related_name="report"
    )
    counselor = models.ForeignKey(
        CounselorProfile, on_delete=models.CASCADE, related_name="reports"
    )
    summary = models.TextField(blank=True, default="")
    recommendations = models.TextField(blank=True, default="")
    next_steps = models.TextField(
        blank=True, default="",
        help_text="Recommended next steps / follow-up plan.",
    )
    attachment = models.FileField(
        upload_to="counselors/reports/", null=True, blank=True
    )
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Report for appointment {self.appointment_id}"
