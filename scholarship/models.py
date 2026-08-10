import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


# -------------------------------------------------------
# 1️⃣ SETTINGS — singleton, mirrors global_settings.GlobalSettings
# -------------------------------------------------------

class ScholarshipSettings(models.Model):
    """Every admin-tunable knob for the Instant Scholarship module. One row,
    pk always 1 (same pattern as global_settings.GlobalSettings)."""

    POLICY_PER_YEAR = "per_year"
    ELIGIBILITY_POLICY_CHOICES = [
        (POLICY_PER_YEAR, "One attempt per verified person per academic year"),
    ]

    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )

    enabled = models.BooleanField(
        default=True,
        help_text="Master switch for the whole scholarship module.",
    )

    # ── Exam shape ──────────────────────────────────────────────────────
    question_count = models.PositiveSmallIntegerField(default=50)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    difficulty_easy_pct = models.PositiveSmallIntegerField(default=60)
    difficulty_medium_pct = models.PositiveSmallIntegerField(default=30)
    difficulty_hard_pct = models.PositiveSmallIntegerField(default=10)

    # ── Eligibility ─────────────────────────────────────────────────────
    # Only one policy exists today (locked in by product decision: one
    # verified person = one attempt per academic year, regardless of class).
    # Kept as a choice field rather than a bare constant so a second policy
    # can be added later without a schema change — but do not build a second
    # policy's enforcement path until one is actually needed.
    eligibility_policy = models.CharField(
        max_length=20, choices=ELIGIBILITY_POLICY_CHOICES, default=POLICY_PER_YEAR
    )
    # Academic year "ends" on this month/day; an award earned in year Y is
    # valid for redemption until then. Matches accounts.LearnerProfile's
    # free-text academic_year values (e.g. "2026-27") loosely — the END date
    # is what actually matters for award expiry.
    award_valid_until_month = models.PositiveSmallIntegerField(default=5)  # May
    award_valid_until_day = models.PositiveSmallIntegerField(default=31)

    # ── Identity verification (parent/guardian-anchored — see
    # memory/instant-scholarship-module-scoping.md for why) ───────────────
    allow_digilocker = models.BooleanField(default=True)
    # Requires a licensed reseller (see active_kyc_provider below) — a
    # documented stub until one is wired. Default off since turning it on
    # today would offer a method that can never actually complete.
    allow_aadhaar_otp = models.BooleanField(default=False)
    # Free, no vendor, no AUA licence — verified via UIDAI's own published
    # signing certificate. See scholarship/aadhaar_offline.py.
    allow_aadhaar_offline = models.BooleanField(default=True)
    allow_manual_review = models.BooleanField(default=True)
    active_kyc_provider = models.CharField(
        max_length=30, blank=True,
        help_text=(
            "Licensed reseller handling DigiLocker/Aadhaar OTP calls "
            "(e.g. setu, digio, surepass, hyperverge). ShikshaCom must never "
            "call UIDAI directly or store an Aadhaar number/hash — only the "
            "reseller's opaque, non-reversible verification reference."
        ),
    )

    # ── Anti-cheat (server-owned; see README's 'strong but invisible' brief) ─
    enable_device_fingerprint = models.BooleanField(default=True)
    enable_tab_switch_tracking = models.BooleanField(default=True)
    tab_switch_flag_threshold = models.PositiveSmallIntegerField(
        default=5, help_text="Tab-switches at/above this count auto-flags the session for admin review."
    )
    answer_burst_seconds_threshold = models.PositiveSmallIntegerField(
        default=3, help_text="Answering faster than this, repeatedly, is logged as a suspicious burst."
    )
    answer_burst_count_threshold = models.PositiveSmallIntegerField(
        default=10, help_text="Number of burst-fast answers in one session before it is auto-flagged."
    )
    # Off by default: product decision was "strong but invisible", no manual
    # review gate on top bands. Left admin-toggleable since it's cheap
    # insurance an admin may want later without a code change.
    auto_review_top_bands = models.BooleanField(default=False)
    top_band_review_min_pct = models.PositiveSmallIntegerField(default=40)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scholarship settings"
        verbose_name_plural = "Scholarship settings"

    def __str__(self):
        return f"Scholarship settings (enabled={self.enabled})"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        super().save(*args, **kwargs)

    def clean(self):
        total = self.difficulty_easy_pct + self.difficulty_medium_pct + self.difficulty_hard_pct
        if total != 100:
            raise ValidationError(
                f"Difficulty split must sum to 100 (got {total})."
            )

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# -------------------------------------------------------
# 2️⃣ SCHOLARSHIP BANDS — admin-editable score→discount table
# -------------------------------------------------------

class ScholarshipBand(models.Model):
    """One row of the scoring table (e.g. 45-49 correct → 40%). Evaluated
    highest-first at scoring time. Admin-editable so bands can change
    without a deploy; a session's award snapshots the % at scoring time so
    editing bands later never rewrites a past student's award."""

    min_correct = models.PositiveSmallIntegerField()
    max_correct = models.PositiveSmallIntegerField()
    discount_pct = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-min_correct"]
        constraints = [
            models.UniqueConstraint(
                fields=["min_correct", "max_correct"], name="scholarship_band_unique_range"
            ),
        ]

    def __str__(self):
        return f"{self.min_correct}-{self.max_correct} correct → {self.discount_pct}%"

    def clean(self):
        if self.min_correct > self.max_correct:
            raise ValidationError("min_correct cannot exceed max_correct.")


# -------------------------------------------------------
# 3️⃣ QUESTION BANK — server-side only; correct_option_index never leaves this app
# -------------------------------------------------------

class ScholarshipQuestionBankItem(models.Model):
    SUBJECT_MATHEMATICS = "mathematics"
    SUBJECT_SCIENCE = "science"
    SUBJECT_ENGLISH = "english"
    SUBJECT_SOCIAL_STUDIES = "social_studies"
    SUBJECT_GENERAL_KNOWLEDGE = "general_knowledge"
    SUBJECT_CURRENT_AFFAIRS = "current_affairs"
    SUBJECT_CHOICES = [
        (SUBJECT_MATHEMATICS, "Mathematics"),
        (SUBJECT_SCIENCE, "Science"),
        (SUBJECT_ENGLISH, "English"),
        (SUBJECT_SOCIAL_STUDIES, "Social Studies"),
        (SUBJECT_GENERAL_KNOWLEDGE, "General Knowledge"),
        (SUBJECT_CURRENT_AFFAIRS, "Current Affairs"),
    ]
    # Explicitly excluded per business rules: Mizo, Manipuri, Hindi and other
    # regional-language subjects. Not a DB constraint — just don't add them.

    DIFFICULTY_EASY = "easy"
    DIFFICULTY_MEDIUM = "medium"
    DIFFICULTY_HARD = "hard"
    DIFFICULTY_CHOICES = [
        (DIFFICULTY_EASY, "Easy"),
        (DIFFICULTY_MEDIUM, "Medium"),
        (DIFFICULTY_HARD, "Challenging"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_AI_GENERATED = "ai_generated"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manually written"),
        (SOURCE_AI_GENERATED, "AI-generated"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class_level = models.PositiveSmallIntegerField(help_text="8-12, matches courses.Course.class_level")
    subject = models.CharField(max_length=30, choices=SUBJECT_CHOICES)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES)

    text = models.TextField()
    # Exactly 4 strings, canonical order. Shuffled per-student at exam
    # generation time — this row's order is never what a student sees.
    options = models.JSONField()
    correct_option_index = models.PositiveSmallIntegerField()
    explanation = models.TextField(blank=True)

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive items are excluded from generation without deleting exam history that references them.",
    )
    # AI-generated items land inactive until an admin reviews and activates them.
    source = models.CharField(max_length=15, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["class_level", "subject", "difficulty", "is_active"]),
        ]

    def __str__(self):
        return f"[{self.class_level}/{self.subject}/{self.difficulty}] {self.text[:60]}"

    def clean(self):
        if not isinstance(self.options, list) or len(self.options) != 4:
            raise ValidationError("options must be a list of exactly 4 strings.")
        if not (0 <= self.correct_option_index <= 3):
            raise ValidationError("correct_option_index must be 0-3.")


# -------------------------------------------------------
# 4️⃣ GUARDIAN VERIFICATION — the ADULT's identity, never the minor's
# -------------------------------------------------------

class GuardianVerification(models.Model):
    """Identity verification is anchored on the parent/guardian account, not
    the student. Two independent reasons (see memory for full citations):

    - DPDP Act 2023 §9 requires verifiable parental consent to process a
      child's (under-18) personal data. Rule 10 explicitly names a
      DigiLocker-issued virtual token as an authorised consent mechanism —
      this model's `provider_reference` field is designed around that.
    - Aadhaar OTP e-KYC on a minor is both legally awkward and practically
      weak (most Class 8-12 students don't have an independent
      Aadhaar-linked account). Verifying the adult sidesteps both problems.

    NEVER store an Aadhaar number or a hash of it here, even for dedup — the
    UIDAI Aadhaar Data Vault obligation (and Aadhaar Act ss.37/38 penalties)
    attaches to *any* entity storing the number electronically, not just
    licensed AUAs. `provider_reference` must be the licensed reseller's own
    opaque, non-reversible token — confirm that with the vendor's docs
    before wiring a new one in.
    """

    METHOD_DIGILOCKER = "digilocker"
    METHOD_AADHAAR_OTP = "aadhaar_otp"
    METHOD_AADHAAR_OFFLINE = "aadhaar_offline"
    METHOD_MANUAL = "manual"
    METHOD_CHOICES = [
        (METHOD_DIGILOCKER, "DigiLocker"),
        (METHOD_AADHAAR_OTP, "Aadhaar OTP"),
        # The only Aadhaar-based path that's actually wired up today — see
        # scholarship/aadhaar_offline.py. METHOD_AADHAAR_OTP remains a
        # documented stub (needs a paid licensed reseller); this one needs
        # no vendor and no AUA licence, verified via UIDAI's own published
        # signing certificate.
        (METHOD_AADHAAR_OFFLINE, "Aadhaar (Offline e-KYC)"),
        (METHOD_MANUAL, "Manual document review"),
    ]

    STATUS_PENDING = "pending"
    STATUS_VERIFIED = "verified"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_VERIFIED, "Verified"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The parent/guardian's own account — the adult completing verification
    # and giving DPDP consent on the child's behalf.
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guardian_verifications"
    )

    method = models.CharField(max_length=15, choices=METHOD_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)

    provider = models.CharField(
        max_length=30, blank=True,
        help_text="Licensed KYC reseller used (setu/digio/surepass/...). Blank for manual.",
    )
    provider_reference = models.CharField(
        max_length=200, blank=True,
        help_text="Opaque, non-reversible verification token from the reseller. Never the Aadhaar number.",
    )

    verified_adult_name = models.CharField(max_length=200, blank=True)
    verified_adult_dob = models.DateField(null=True, blank=True)

    manual_document = models.FileField(
        upload_to="scholarship/guardian_docs/%Y/%m/", null=True, blank=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=300, blank=True)

    # DPDP Rule 10 consent audit trail: who consented, when, from where.
    consent_given_at = models.DateTimeField(null=True, blank=True)
    consent_ip = models.GenericIPAddressField(null=True, blank=True)
    consent_user_agent = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["account", "status"])]

    def __str__(self):
        return f"{self.account.email} [{self.method}/{self.status}]"

    @property
    def dedup_reference(self):
        """The stable string used to build the eligibility dedup hash.
        Prefers the reseller's opaque token; falls back to the verified
        adult's own account email for the manual-review path, where no
        third-party token exists."""
        return self.provider_reference or f"manual:{self.account_id}"


# -------------------------------------------------------
# 5️⃣ ELIGIBILITY LEDGER — the actual anti-spam enforcement
# -------------------------------------------------------

class ScholarshipEligibilityRecord(models.Model):
    """One row per (real person, academic year). The UniqueConstraint below
    is the actual anti-fraud mechanism — the database itself refuses a
    second active row for the same dedup_hash + academic_year, so no number
    of new email accounts can grant a second attempt to the same real
    student in the same year. Siblings under one parent still each get a
    row, since the hash includes the child's own name+DOB."""

    STATUS_RESERVED = "reserved"
    STATUS_CONSUMED = "consumed"
    STATUS_VOIDED = "voided"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Reserved (exam in progress)"),
        (STATUS_CONSUMED, "Consumed (exam submitted)"),
        (STATUS_VOIDED, "Voided by admin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # sha256 of a server-side pepper + the guardian's dedup_reference + the
    # child's normalized name + DOB. See services.compute_dedup_hash — never
    # store the raw Aadhaar number here, even hashed alone (no pepper), since
    # an unsalted hash of a 12-digit number is realistically reversible.
    dedup_hash = models.CharField(max_length=64, db_index=True)

    academic_year = models.CharField(max_length=20)

    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile", on_delete=models.CASCADE, related_name="scholarship_eligibility_records"
    )
    guardian_verification = models.ForeignKey(
        GuardianVerification, on_delete=models.PROTECT, related_name="eligibility_records"
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Active = reserved or consumed. A voided row frees up the slot
            # (e.g. after admin confirms it was a data-entry error), which is
            # why this isn't a plain unique_together on the two fields.
            models.UniqueConstraint(
                fields=["dedup_hash", "academic_year"],
                # Literal strings, not STATUS_RESERVED/STATUS_CONSUMED: Meta's
                # body executes before the class exists, so the class-level
                # constants aren't reachable here yet.
                condition=Q(status__in=["reserved", "consumed"]),
                name="scholarship_one_active_attempt_per_year",
            ),
        ]
        indexes = [models.Index(fields=["learner_profile", "academic_year"])]

    def __str__(self):
        return f"{self.dedup_hash[:8]}… / {self.academic_year} [{self.status}]"


# -------------------------------------------------------
# 6️⃣ EXAM SESSION — server-owned timer, the deadline is set once and never moved
# -------------------------------------------------------

class ExamSession(models.Model):
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_SUBMITTED = "submitted"
    STATUS_EXPIRED = "expired"
    STATUS_VOIDED = "voided"
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_EXPIRED, "Expired (auto-submitted at deadline)"),
        (STATUS_VOIDED, "Voided by admin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile", on_delete=models.CASCADE, related_name="scholarship_exam_sessions"
    )
    course = models.ForeignKey(
        "courses.Course", on_delete=models.PROTECT, related_name="scholarship_exam_sessions"
    )
    eligibility_record = models.OneToOneField(
        ScholarshipEligibilityRecord, on_delete=models.PROTECT, related_name="exam_session"
    )

    started_at = models.DateTimeField(auto_now_add=True)
    # Set once, at creation, from ScholarshipSettings.duration_minutes.
    # Never mutated after creation — the client renders a countdown FROM this
    # value but the server is the only thing that enforces it (see
    # services.submit_exam / tasks.expire_exam_sessions).
    deadline = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_IN_PROGRESS)

    score = models.PositiveSmallIntegerField(null=True, blank=True)
    # Snapshot of the band table AT SCORING TIME, so editing
    # ScholarshipBand later never rewrites a past student's result.
    awarded_discount_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    subject_breakdown = models.JSONField(default=dict, blank=True)

    # ── Anti-cheat signal aggregates (raw events live in CheatSignalEvent) ──
    tab_switch_count = models.PositiveSmallIntegerField(default=0)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    device_fingerprint = models.CharField(max_length=128, blank=True)

    flagged_for_review = models.BooleanField(default=False)
    REVIEW_CLEARED = "cleared"
    REVIEW_VOIDED = "voided"
    REVIEW_STATUS_CHOICES = [(REVIEW_CLEARED, "Cleared"), (REVIEW_VOIDED, "Voided")]
    review_status = models.CharField(max_length=10, choices=REVIEW_STATUS_CHOICES, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "deadline"]),
            models.Index(fields=["learner_profile"]),
            models.Index(fields=["flagged_for_review"]),
        ]

    def __str__(self):
        return f"{self.learner_profile} · {self.course} [{self.status}]"

    @property
    def is_past_deadline(self):
        return timezone.now() >= self.deadline


# -------------------------------------------------------
# 7️⃣ EXAM QUESTION — per-session denormalized snapshot, shuffled per student
# -------------------------------------------------------

class ExamQuestion(models.Model):
    """A copy of a bank item's content, frozen at generation time and
    shuffled for this specific student. Denormalized deliberately: an admin
    editing the source bank item after a paper has been generated must never
    change what a student already sitting the exam sees, and
    correct_option_index must never be reachable from a student-facing
    query — see scholarship/serializers.py ExamQuestionStudentSerializer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveSmallIntegerField(help_text="0-indexed position in this student's paper.")

    source_item = models.ForeignKey(
        ScholarshipQuestionBankItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    subject = models.CharField(max_length=30)
    difficulty = models.CharField(max_length=10)
    text = models.TextField()
    options = models.JSONField(help_text="4 strings, in the shuffled order shown to this student.")
    correct_option_index = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["session", "order"], name="scholarship_examquestion_unique_order"),
        ]

    def __str__(self):
        return f"Q{self.order + 1} · {self.session_id}"


class ExamAnswer(models.Model):
    """The student's response to one ExamQuestion. Kept separate from
    ExamQuestion (rather than folded in) so the full 50-question paper can be
    bulk-created up front at session start, with answers filled in later via
    autosave — mirrors quizzes.Question/StudentAnswer's split."""

    question = models.OneToOneField(ExamQuestion, on_delete=models.CASCADE, related_name="answer")
    selected_option_index = models.PositiveSmallIntegerField(null=True, blank=True)
    is_correct = models.BooleanField(null=True, blank=True)

    answered_at = models.DateTimeField(null=True, blank=True)
    time_spent_seconds = models.PositiveIntegerField(default=0)
    # How many times the student changed their answer on this question —
    # not itself a cheat signal, just useful telemetry for anomaly review.
    change_count = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f"Answer to {self.question_id}"


# -------------------------------------------------------
# 8️⃣ CHEAT SIGNALS — the audit trail (no generic one exists elsewhere)
# -------------------------------------------------------

class CheatSignalEvent(models.Model):
    EVENT_TAB_HIDDEN = "tab_hidden"
    EVENT_FOCUS_LOST = "focus_lost"
    EVENT_PASTE_DETECTED = "paste_detected"
    EVENT_DEVTOOLS_SUSPECTED = "devtools_suspected"
    EVENT_ANSWER_BURST = "answer_burst"
    EVENT_IP_CHANGED = "ip_changed"
    EVENT_MULTI_DEVICE = "multi_device"
    EVENT_CHOICES = [
        (EVENT_TAB_HIDDEN, "Tab hidden"),
        (EVENT_FOCUS_LOST, "Window focus lost"),
        (EVENT_PASTE_DETECTED, "Paste detected"),
        (EVENT_DEVTOOLS_SUSPECTED, "DevTools suspected"),
        (EVENT_ANSWER_BURST, "Answer burst (suspiciously fast)"),
        (EVENT_IP_CHANGED, "IP address changed mid-session"),
        (EVENT_MULTI_DEVICE, "Multiple devices detected"),
    ]

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="cheat_signals")
    event_type = models.CharField(max_length=25, choices=EVENT_CHOICES)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["session", "event_type"])]

    def __str__(self):
        return f"{self.event_type} @ {self.session_id}"


# -------------------------------------------------------
# 9️⃣ AWARD — the redeemable artifact, independent lifecycle from ExamSession
# -------------------------------------------------------

class ScholarshipAward(models.Model):
    """Locked while the platform is in free-launch mode (informational,
    shown to the student, redeems for ₹0 through enrollments.FreeEnrollView
    like everything else); becomes a real discount the moment
    GlobalSettings.free_trial_enabled flips off — see services.get_active_award
    and enrollments/payment_views.py's integration point."""

    STATUS_LOCKED = "locked"
    STATUS_ACTIVE = "active"
    STATUS_REDEEMED = "redeemed"
    STATUS_EXPIRED = "expired"
    STATUS_VOIDED = "voided"
    STATUS_CHOICES = [
        (STATUS_LOCKED, "Locked (earned during free launch)"),
        (STATUS_ACTIVE, "Active (real discount, redeemable)"),
        (STATUS_REDEEMED, "Redeemed"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_VOIDED, "Voided by admin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile", on_delete=models.CASCADE, related_name="scholarship_awards"
    )
    exam_session = models.OneToOneField(ExamSession, on_delete=models.PROTECT, related_name="award")
    # Locked to the course chosen at exam time — "Scholarship valid for this
    # course only" per the design brief, not a general-purpose coupon.
    course = models.ForeignKey("courses.Course", on_delete=models.PROTECT, related_name="scholarship_awards")

    discount_pct = models.PositiveSmallIntegerField()
    academic_year = models.CharField(max_length=20)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_LOCKED)
    expires_at = models.DateTimeField()

    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_enrollment = models.ForeignKey(
        "enrollments.Enrollment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    redeemed_subscription = models.ForeignKey(
        "enrollments.Subscription", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["learner_profile", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]

    def __str__(self):
        return f"{self.learner_profile} · {self.course} · {self.discount_pct}% [{self.status}]"

    @property
    def is_redeemable(self):
        return (
            self.status in (self.STATUS_ACTIVE, self.STATUS_LOCKED)
            and self.expires_at > timezone.now()
        )
