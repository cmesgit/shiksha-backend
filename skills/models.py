# PLACEMENT: skills/models.py  (replace the whole file)
# that migration 0003 created but the model had lost) + aligns slot_key help_text.
# No new migration: the model now matches 0003/0004 exactly.
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
    image = models.ImageField(upload_to="skills/categories/", null=True, blank=True)
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
    # An expert can teach MORE THAN ONE subject. `categories` is the full set;
    # `category` is kept as the primary subject for backward compatibility and
    # is always synced to the first entry of `categories`.
    categories = models.ManyToManyField(
        SkillCategory, blank=True, related_name="multi_experts"
    )

    headline = models.CharField(max_length=160)                # "Web Developer · ex-Infosys"
    skill_tags = models.JSONField(default=list, blank=True)    # ["React", "Node.js"]
    bio = models.TextField(blank=True)
    availability = models.CharField(max_length=120, blank=True)
    # Weekly bookable grid driving the Book-a-Tutor calendar + the expert's
    # own Availability screen. Shape: {"open": ["0-1", ...], "booked": ["1-0", ...]}
    # where each key is "<dayIndex>-<slotIndex>". This field is added by
    # migration 0003_expertprofile_availability_slots; it MUST stay defined on
    # the model (without it, every availability read/write silently breaks).
    availability_slots = models.JSONField(
        blank=True,
        default=dict,
        help_text='Weekly availability. Shape: {"open":["0-1","2-3"], "booked":["1-0"]}',
    )
    badges = models.JSONField(default=list, blank=True)        # ["Verified", "Top-rated"]
    photo = models.ImageField(upload_to="skills/experts/", null=True, blank=True)

    # ── Intro video (advertising, not a session recording) ─────────────────
    # One short Bunny-hosted clip advertising what this expert teaches. Kept
    # inline on the profile (like `photo`) rather than a separate model —
    # there is exactly one per expert, with no FK relationships to model.
    INTRO_VIDEO_STATUS_CHOICES = [
        (0, "Created"),
        (1, "Uploaded"),
        (2, "Processing"),
        (3, "Transcoding"),
        (4, "Finished"),
        (5, "Error"),
    ]
    intro_video_bunny_id = models.CharField(max_length=255, blank=True)
    intro_video_status = models.IntegerField(
        choices=INTRO_VIDEO_STATUS_CHOICES, null=True, blank=True
    )
    intro_video_thumbnail_url = models.URLField(blank=True)

    # Rate is stored in paise for consistency with courses/payments.
    hourly_rate = models.PositiveIntegerField(default=0, help_text="Paise (₹1 = 100)")

    # ── Mastery ─────────────────────────────────────────────────────────
    # "Complete N sessions with me to master my course." Progress itself is
    # NEVER stored — it's always `SkillSession.objects.filter(expert=self,
    # learner_profile=<student>, status=COMPLETED).count()` vs this target,
    # so changing the target re-derives every student's status immediately.
    mastery_target = models.PositiveSmallIntegerField(
        default=3,
        help_text="Sessions a student must complete with this expert to reach mastery (1-12).",
    )

    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    sessions_count = models.PositiveIntegerField(default=0)

    is_listed = models.BooleanField(
        default=False,
        help_text="Approved expert appears in the directory. FREE — never gated "
                  "by subscription. Gates session/booking visibility, so it must "
                  "stay True for every approved expert.",
    )

    # ── Advertising (subscription-gated) ──────────────────────────────────
    # `is_listed` = in the directory for free. `is_featured` = paid advertising
    # is currently active (homepage promotion + reach boost). In the free launch
    # phase (GlobalSettings.effective_mode == 'free') everyone is advertised
    # regardless of `is_featured`; once billing switches on, only experts with
    # an active ad-subscription are advertised. See `is_advertised`.
    is_featured = models.BooleanField(default=False)
    featured_since = models.DateTimeField(null=True, blank=True)
    # Visibility score. Grows while advertised / on completed sessions, and is
    # decayed when an ad-subscription is cancelled or lapses.
    reach_count = models.PositiveIntegerField(default=0)
    # Admin suspension: hard-off switch. When True the expert is delisted, can't
    # take new bookings, isn't advertised, and sync_listing() will NOT re-list
    # them until an admin lifts the suspension.
    is_suspended = models.BooleanField(default=False)

    # ── Offline-class location (for "find a tutor near me") ───────────────
    # Surfaced to learners searching for someone who can teach offline. Exact
    # location is required when class_mode is "home" or "travel" (validated in
    # the profile-update view).
    MODE_HOME = "home"
    MODE_TRAVEL = "travel"
    MODE_ONLINE = "online"
    CLASS_MODE_CHOICES = [
        (MODE_HOME, "At my place"),
        (MODE_TRAVEL, "I can travel to the learner"),
        (MODE_ONLINE, "Online only"),
    ]
    class_mode = models.CharField(
        max_length=10, choices=CLASS_MODE_CHOICES, default=MODE_ONLINE
    )
    class_location = models.CharField(
        max_length=255, blank=True,
        help_text="Exact location text; required when class_mode is home/travel.",
    )
    pincode = models.CharField(max_length=10, blank=True)
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=150, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # ── Teaching profile extras ───────────────────────────────────────────
    languages = models.JSONField(default=list, blank=True)   # ["English","Hindi"]
    subject_description = models.TextField(blank=True)

    # ── Expert Profile screen extras (design_handoff_skilldev) ────────────
    # Nothing modeled these before this redesign's Expert Profile page,
    # which shows a "Years experience" stat, an Experience timeline, and an
    # Education line none of the existing fields cover.
    experience_years = models.PositiveSmallIntegerField(null=True, blank=True)
    education = models.CharField(max_length=160, blank=True)
    # [{"years": "2021 — now", "role": "Lead Data Scientist, Flipkart",
    #   "detail": "Recommendation and pricing models serving 40M+ users."}, ...]
    experience_timeline = models.JSONField(default=list, blank=True)

    # ── Direct (P2P) payment details ──────────────────────────────────────
    # Booking/course money is settled DIRECTLY between the learner and the
    # expert — the platform never collects it. These are the expert's own payee
    # details, shown to a learner after booking so they can pay the expert.
    payment_upi = models.CharField(
        max_length=120, blank=True, help_text="Expert's own UPI ID (learners pay here)."
    )
    payment_name = models.CharField(max_length=120, blank=True)
    payment_note = models.CharField(max_length=200, blank=True)

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

    # ── Advertising / reach ────────────────────────────────────────────────
    @staticmethod
    def billing_is_free():
        """True when the platform is in its free launch phase (no charges)."""
        try:
            from global_settings.models import GlobalSettings
            return GlobalSettings.load().effective_mode == GlobalSettings.PAYMENT_FREE
        except Exception:
            # Fail open to FREE so a missing settings row never hides experts.
            return True

    def is_advertised(self):
        """Whether this expert is promoted on the homepage right now.

        Free phase  → every listed expert is advertised (no subscription needed).
        Paid phase  → only experts with an active ad-subscription (is_featured).
        """
        if not self.is_listed:
            return False
        if self.billing_is_free():
            return True
        return self.is_featured

    def add_reach(self, amount):
        if amount:
            self.reach_count = (self.reach_count or 0) + int(amount)
            self.save(update_fields=["reach_count", "updated_at"])

    def decay_reach(self, factor=0.5):
        """Drop reach when advertising stops (subscription cancelled/lapsed)."""
        self.reach_count = int((self.reach_count or 0) * factor)
        self.save(update_fields=["reach_count", "updated_at"])

    # ── Profile completeness + auto-listing (guest-expert onboarding gate) ──
    # The signup flow and the dashboard editor both decide "is this profile
    # filled?" through here, so they can never disagree. An expert is listed in
    # the public directory only once complete — a half-finished signup stays
    # hidden and the dashboard forces the profile screen until it's done.
    def completeness(self):
        """{"is_complete": bool, "missing": [field_key, ...]}.

        Field keys are stable identifiers the frontend maps to labels. Personal
        fields (name/dob/phone/photo) are read from the SELF learner profile."""
        from . import profile_ops as ops
        lp = self.teacher_profile.user.default_learner_profile()

        has_subjects = bool(self.category_id) or (
            self.pk and self.categories.exists()
        )
        missing = ops.expert_missing(
            category_id=self.category_id,
            has_subjects=has_subjects,
            subject_description=self.subject_description,
            languages=self.languages,
            bio=self.bio,
            hourly_rate=self.hourly_rate // 100,   # stored in paise
            class_mode=self.class_mode,
            class_location=self.class_location,
        )
        missing += ops.personal_missing(
            full_name=(lp.full_name if lp else ""),
            first_name=(lp.first_name if lp else ""),
            last_name=(lp.last_name if lp else ""),
            date_of_birth=(lp.date_of_birth if lp else None),
            phone=(lp.phone if lp else ""),
            profile_photo=(lp.profile_photo if lp else None) or self.photo,
        )
        return {"is_complete": not missing, "missing": missing}

    @property
    def is_complete(self):
        return self.completeness()["is_complete"]

    def refresh_listing(self, *, save=True):
        """List the expert (free) once their profile is complete. Never
        UN-lists an already-listed profile, so an approved expert who later
        blanks a field keeps their listing while the dashboard nudges them to
        fix it. Returns the resulting ``is_listed``."""
        if self.is_complete and not self.is_listed and not self.is_suspended:
            self.is_listed = True
            if save:
                self.save(update_fields=["is_listed", "updated_at"])
        return self.is_listed

    def has_offline_class(self):
        return self.class_mode in (self.MODE_HOME, self.MODE_TRAVEL)

    def pay_to(self):
        """Direct-payment payee block shown to a learner, or None if unset."""
        if not (self.payment_upi or self.payment_name):
            return None
        return {
            "upi":  self.payment_upi,
            "name": self.payment_name or self.display_name(),
            "note": self.payment_note,
        }

    def intro_video_ready(self):
        return self.intro_video_status == 4  # Finished

    def intro_video_embed_url(self):
        """Playable Bunny embed URL, or None if there's no finished video."""
        if not (self.intro_video_bunny_id and self.intro_video_ready()):
            return None
        from django.conf import settings
        return f"{settings.BUNNY_EMBED}/{settings.BUNNY_LIBRARY_ID}/{self.intro_video_bunny_id}"

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
    STATUS_NEEDS_RECONFIRMATION = "needs_reconfirmation"  # == design's "awaiting_student"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    # A teacher explicitly declining a pending REQUEST (Bookings › Requests).
    # Distinct from CANCELLED, which is a learner backing out of an already-
    # scheduled session — the design's Requests tab needs to tell these apart.
    STATUS_DECLINED = "declined"
    # The 24h SLA on a request expired with no teacher response — same
    # student-facing outcome as DECLINED (refunded, notified) but a distinct
    # status so "declined" vs "expired unanswered" isn't lost.
    STATUS_AUTO_DECLINED = "auto_declined"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_NEEDS_RECONFIRMATION, "Needs reconfirmation"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_AUTO_DECLINED, "Auto-declined (24h SLA)"),
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
    # Set the moment the EXPERT enters the room (clicks "Start class"). The
    # learner's dashboard reads this to surface a live "Join now" signal, since
    # the expert may start at any time — not just inside the scheduled window.
    started_at = models.DateTimeField(null=True, blank=True)
    duration_mins = models.PositiveIntegerField(default=60)
    # The availability slot this session reserved, e.g. "3-1" (day-slot index).
    # Stored so the slot can be released back to the expert's `open` grid when
    # the session is cancelled / declined / completed.
    slot_key = models.CharField(
        max_length=16, blank=True,
        help_text="Reserved availability slot, e.g. '3-1' (day-slot index).",
    )

    # ── Reschedule proposal (teacher proposes, learner confirms/declines) ──
    # Mirrors sessions_app.PrivateSession's rescheduled_date/time, but this
    # model books a slot_key against the expert's weekly grid rather than a
    # separate date+time pair, so the proposal is a candidate slot_key +
    # its derived scheduled_for, not two loose date/time fields.
    proposed_slot_key = models.CharField(max_length=16, blank=True)
    proposed_scheduled_for = models.DateTimeField(null=True, blank=True)
    reschedule_reason = models.CharField(max_length=255, blank=True)
    # The status this session was in right before a reschedule was proposed
    # (REQUESTED or CONFIRMED — see RESCHEDULABLE). Declining the proposal
    # reverts to exactly this per WORKFLOW.md §3 ("Keep original → reverts to
    # previous status"), rather than cancelling the whole session.
    status_before_reschedule = models.CharField(max_length=20, blank=True)

    # ── No-show ─────────────────────────────────────────────────────────
    # Teacher-reported: the student didn't turn up. Per WORKFLOW.md §4 the
    # session is still forfeited/paid in full — no_show is a flag alongside
    # STATUS_COMPLETED, not a separate status.
    no_show = models.BooleanField(default=False)
    no_show_reported_at = models.DateTimeField(null=True, blank=True)

    amount = models.PositiveIntegerField(default=0, help_text="Paise")
    note = models.TextField(blank=True)            # the contact draft / message
    # Teacher's own private note about this session (Bookings › Past "+Note",
    # and the most recent non-blank one surfaces on the Students mastery
    # tracker card). Never visible to the student.
    teacher_note = models.TextField(blank=True)
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


def mastery_progress(expert, learner_profile):
    """{"progress": int, "target": int, "mastered": bool} for one (student,
    teacher) pair. Progress is always derived from completed sessions, never
    stored — see ExpertProfile.mastery_target."""
    progress = SkillSession.objects.filter(
        expert=expert, learner_profile=learner_profile, status=SkillSession.STATUS_COMPLETED,
    ).count()
    target = expert.mastery_target
    return {"progress": progress, "target": target, "mastered": progress >= target}


# =====================================================
# CONTACT THREAD  (learner <-> expert messaging)  — REMOVED
# =====================================================
# The old REST-based Conversation + Message models lived here. They are GONE:
# all 1-on-1 messaging now runs through the realtime WebSocket `chat/` app
# (chat.Conversation / chat.Message), which every frontend already uses. The
# tables are dropped by migration 0005_remove_skill_messaging. messaging_views.py
# and its /skill/conversations/ routes were removed in the same change.


# ── Additive models (separate files, imported here so Django discovers them) ──
# These lines are the ONLY change to this file. The models live in their own
# modules so they can be added without touching the original models above.
from .course_models import (  # noqa: F401, E402
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment, SkillLectureProgress,
)
from .review_models import ExpertReview  # noqa: F401, E402
from .payment_models import SkillPaymentRequest  # noqa: F401, E402
from .subscription_models import ExpertAdSubscription  # noqa: F401, E402
from .marketing_models import SkillMarketingBlock  # noqa: F401, E402
from .attendance_models import (  # noqa: F401, E402
    SkillSessionAttendance, SkillSessionAttendanceInterval,
)
from .blackout_models import ExpertBlackoutDate  # noqa: F401, E402
