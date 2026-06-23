"""
skills/models.py — the specialized-skills domain.

Two halves:
  1. The guest-expert SCREENING pipeline (application -> eligibility ->
     interview -> decision), matching the StAGES/RUBRIC/TIERS from your
     design's Screening flow.
  2. The MARKETPLACE: a directory of approved experts, learner-initiated
     sessions (booking + payment), and a light contact thread.

Everything links back to the accounts app: an applicant is a User, an
approved expert wraps that User's TeacherProfile, and a session is booked
by a LearnerProfile.
"""
import uuid

from django.conf import settings
from django.db import models


# =====================================================
# CATEGORY  (the 8+ skill categories)
# =====================================================

class SkillCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=40, unique=True)        # "coding", "music"
    label = models.CharField(max_length=80)                    # "Coding & Web"
    icon = models.CharField(max_length=8, blank=True)          # glyph used on cards
    color = models.CharField(max_length=9, blank=True)         # hex accent
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "label"]
        verbose_name_plural = "Skill categories"

    def __str__(self):
        return self.label


# =====================================================
# EXPERT PROFILE  (one per approved guest-expert teacher)
# =====================================================
#
# Wraps accounts.TeacherProfile with the marketplace-facing card data.
# Only listed (is_listed=True) after the screening panel approves.

class ExpertProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    teacher_profile = models.OneToOneField(
        "accounts.TeacherProfile",
        on_delete=models.CASCADE,
        related_name="expert_profile",
    )

    category = models.ForeignKey(
        SkillCategory, on_delete=models.SET_NULL, null=True, related_name="experts"
    )

    headline = models.CharField(max_length=160)                # "Web Developer · ex-Infosys"
    skill_tags = models.JSONField(default=list, blank=True)    # ["React", "Node.js"]
    bio = models.TextField(blank=True)
    availability = models.CharField(max_length=120, blank=True)
    badges = models.JSONField(default=list, blank=True)        # ["Verified", "Top-rated"]
    photo = models.ImageField(upload_to="skills/experts/", null=True, blank=True)

    # Rate is stored in paise for consistency with courses/payments.
    hourly_rate = models.PositiveIntegerField(default=0, help_text="Paise (₹1 = 100)")

    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    sessions_count = models.PositiveIntegerField(default=0)

    is_listed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-rating", "-sessions_count"]
        indexes = [models.Index(fields=["is_listed", "category"])]

    @property
    def user(self):
        return self.teacher_profile.user

    @property
    def rate_rupees(self):
        return self.hourly_rate // 100

    def display_name(self):
        u = self.user
        # `user.profile` (old Profile model) was removed in 0011.
        # Use the SELF LearnerProfile instead.
        lp = u.default_learner_profile()
        if lp:
            name = f"{lp.first_name} {lp.last_name}".strip()
            if name:
                return name
            if lp.full_name:
                return lp.full_name
            if lp.display_name:
                return lp.display_name
        return u.username or u.email

    def __str__(self):
        return f"Expert · {self.display_name()}"


# =====================================================
# TEACHER APPLICATION  (screening pipeline)
# =====================================================

class TeacherApplication(models.Model):
    TRACK_GUEST = "GUEST"
    TRACK_FACULTY = "FACULTY"
    TRACK_CHOICES = [
        (TRACK_GUEST, "Guest expert (skills)"),
        (TRACK_FACULTY, "Faculty (academic)"),
    ]

    # Pipeline stages mirror the design: application -> eligibility ->
    # interview -> decision. Status is finer-grained than stage.
    STATUS_SUBMITTED = "submitted"           # stage: application
    STATUS_ELIGIBILITY = "eligibility"       # stage: eligibility (docs/ID)
    STATUS_INTERVIEW_SCHEDULED = "scheduled"  # stage: interview
    STATUS_INTERVIEW_READY = "ready"          # stage: interview (slot reached)
    STATUS_APPROVED = "approved"             # stage: decision
    STATUS_HOLD = "hold"                     # stage: decision
    STATUS_REJECTED = "rejected"             # stage: decision
    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_ELIGIBILITY, "Eligibility check"),
        (STATUS_INTERVIEW_SCHEDULED, "Interview scheduled"),
        (STATUS_INTERVIEW_READY, "Ready for interview"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_HOLD, "On hold"),
        (STATUS_REJECTED, "Rejected"),
    ]

    STAGE_BY_STATUS = {
        STATUS_SUBMITTED: "application",
        STATUS_ELIGIBILITY: "eligibility",
        STATUS_INTERVIEW_SCHEDULED: "interview",
        STATUS_INTERVIEW_READY: "interview",
        STATUS_APPROVED: "decision",
        STATUS_HOLD: "decision",
        STATUS_REJECTED: "decision",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="teacher_applications",
    )
    track = models.CharField(max_length=10, choices=TRACK_CHOICES, default=TRACK_GUEST)

    category = models.ForeignKey(
        SkillCategory, on_delete=models.SET_NULL, null=True, blank=True
    )
    skill_name = models.CharField(max_length=200)
    headline = models.CharField(max_length=160, blank=True)
    experience = models.CharField(max_length=40, blank=True)   # "3-5 years"
    method_note = models.TextField(blank=True)                 # guest "method"
    skill_tags = models.JSONField(default=list, blank=True)

    # Guest experts upload a ~1-minute intro; faculty go straight to interview.
    intro_video = models.FileField(
        upload_to="skills/applications/videos/", null=True, blank=True
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_SUBMITTED
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_applications",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "track"])]

    @property
    def stage(self):
        return self.STAGE_BY_STATUS.get(self.status, "application")

    def __str__(self):
        return f"{self.user.email} · {self.skill_name} ({self.status})"


class InterviewSlot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    starts_at = models.DateTimeField()
    duration_mins = models.PositiveIntegerField(default=30)
    capacity = models.PositiveIntegerField(default=1)
    booked_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["starts_at"]

    @property
    def is_open(self):
        return self.is_active and self.booked_count < self.capacity

    def __str__(self):
        return f"Slot {self.starts_at:%Y-%m-%d %H:%M}"


class Interview(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_COMPLETED = "completed"
    STATUS_NO_SHOW = "no_show"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_NO_SHOW, "No show"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField(
        TeacherApplication, on_delete=models.CASCADE, related_name="interview"
    )
    slot = models.ForeignKey(
        InterviewSlot, on_delete=models.SET_NULL, null=True, blank=True
    )
    scheduled_for = models.DateTimeField()
    meeting_url = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Interview · {self.application.skill_name}"


class Evaluation(models.Model):
    DECISION_APPROVE = "approve"
    DECISION_HOLD = "hold"
    DECISION_REJECT = "reject"
    DECISION_CHOICES = [
        (DECISION_APPROVE, "Approve"),
        (DECISION_HOLD, "Hold"),
        (DECISION_REJECT, "Reject"),
    ]

    # Mirror accounts.TeacherProfile tiers.
    TIER_STANDARD = "standard"
    TIER_SENIOR = "senior"
    TIER_EXPERT = "expert"
    TIER_CHOICES = [
        (TIER_STANDARD, "Standard"),
        (TIER_SENIOR, "Senior"),
        (TIER_EXPERT, "Expert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.OneToOneField(
        Interview, on_delete=models.CASCADE, related_name="evaluation"
    )
    # Rubric scores keyed by RUBRIC slugs: subject/comm/method/engage/prof.
    scores = models.JSONField(default=dict, blank=True)
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES)
    recommended_tier = models.CharField(
        max_length=10, choices=TIER_CHOICES, blank=True
    )
    feedback = models.TextField(blank=True)
    evaluator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="skill_evaluations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Evaluation · {self.decision}"


# =====================================================
# SESSIONS  (learner books an expert) + payment hook
# =====================================================

class SkillSession(models.Model):
    CONTACT_MESSAGE = "message"
    CONTACT_SESSION = "session"
    CONTACT_VIDEO = "video"
    CONTACT_CHOICES = [
        (CONTACT_MESSAGE, "Message"),
        (CONTACT_SESSION, "Session request"),
        (CONTACT_VIDEO, "Video call"),
    ]

    STATUS_REQUESTED = "requested"
    STATUS_PENDING_PAYMENT = "pending_payment"
    STATUS_CONFIRMED = "confirmed"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PAYMENT_UNPAID = "unpaid"
    PAYMENT_PAID = "paid"
    PAYMENT_CHOICES = [
        (PAYMENT_UNPAID, "Unpaid"),
        (PAYMENT_PAID, "Paid"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        related_name="skill_sessions",
    )
    expert = models.ForeignKey(
        ExpertProfile, on_delete=models.CASCADE, related_name="sessions"
    )

    contact_mode = models.CharField(
        max_length=10, choices=CONTACT_CHOICES, default=CONTACT_SESSION
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED
    )

    scheduled_for = models.DateTimeField(null=True, blank=True)
    duration_mins = models.PositiveIntegerField(default=60)
    # The availability slot this session reserved, e.g. "3-1" (day-slot index).
    # Stored so the slot can be released back to the expert's `open` grid when
    # the session is cancelled / declined / completed.
    slot_key = models.CharField(max_length=16, blank=True)
    amount = models.PositiveIntegerField(default=0, help_text="Paise")
    note = models.TextField(blank=True)            # the contact draft / message
    meeting_url = models.CharField(max_length=300, blank=True)

    # Payment (Razorpay) — wired via the payments app.
    payment_status = models.CharField(
        max_length=10, choices=PAYMENT_CHOICES, default=PAYMENT_UNPAID
    )
    razorpay_order_id = models.CharField(max_length=120, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "payment_status"])]

    def __str__(self):
        return f"Session · {self.learner_profile.display_name} → {self.expert.display_name()}"


# =====================================================
# CONTACT THREAD  (learner <-> expert messaging)
# =====================================================

class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile", on_delete=models.CASCADE, related_name="skill_threads"
    )
    expert = models.ForeignKey(
        ExpertProfile, on_delete=models.CASCADE, related_name="threads"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["learner_profile", "expert"],
                name="unique_thread_per_learner_expert",
            )
        ]

    def __str__(self):
        return f"Thread · {self.learner_profile_id} ↔ {self.expert_id}"


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="skill_messages"
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Msg {self.id}"


# ── Additive models (separate files, imported here so Django discovers them) ──
# These lines are the ONLY change to this file. The models live in their own
# modules so they can be added without touching the original models above.
from .course_models import (  # noqa: F401, E402
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment, SkillLectureProgress,
)
from .review_models import ExpertReview  # noqa: F401, E402
from .payment_models import SkillPaymentRequest  # noqa: F401, E402
