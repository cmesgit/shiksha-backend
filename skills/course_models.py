"""
skills/course_models.py — Udemy-style skill courses.

A skill teacher creates a SkillCourse and publishes sections + lectures.
Admin reviews and approves before it appears in the marketplace.
A student (per LearnerProfile) buys/enrolls in a course; progress is
tracked per profile.

Additive — no change to skills/models.py.
"""
import uuid
from django.conf import settings
from django.db import models


class SkillCourse(models.Model):
    STATUS_DRAFT      = "draft"
    STATUS_SUBMITTED  = "submitted"   # teacher submitted for admin review
    STATUS_APPROVED   = "approved"    # live in marketplace
    STATUS_REJECTED   = "rejected"
    STATUS_CHOICES = [
        (STATUS_DRAFT,     "Draft"),
        (STATUS_SUBMITTED, "Submitted for review"),
        (STATUS_APPROVED,  "Approved / live"),
        (STATUS_REJECTED,  "Rejected"),
    ]

    LEVEL_BEGINNER     = "beginner"
    LEVEL_INTERMEDIATE = "intermediate"
    LEVEL_ADVANCED     = "advanced"
    LEVEL_CHOICES = [
        (LEVEL_BEGINNER,     "Beginner"),
        (LEVEL_INTERMEDIATE, "Intermediate"),
        (LEVEL_ADVANCED,     "Advanced"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    teacher_profile = models.ForeignKey(
        "accounts.TeacherProfile",
        on_delete=models.CASCADE,
        related_name="skill_courses",
    )
    category = models.ForeignKey(
        "skills.SkillCategory",
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    title         = models.CharField(max_length=200)
    subtitle      = models.CharField(max_length=300, blank=True)
    description   = models.TextField(blank=True)
    cover_image   = models.ImageField(upload_to="skills/courses/covers/", null=True, blank=True)
    promo_video   = models.URLField(blank=True)       # YouTube / external link
    skill_tags    = models.JSONField(default=list, blank=True)
    level         = models.CharField(max_length=16, choices=LEVEL_CHOICES, default=LEVEL_BEGINNER)
    language      = models.CharField(max_length=60, default="English")
    requirements  = models.JSONField(default=list, blank=True)   # bullet list
    outcomes      = models.JSONField(default=list, blank=True)   # what you'll learn

    # Price in paise (₹1 = 100). 0 means free.
    price = models.PositiveIntegerField(default=0, help_text="Paise (0 = free)")

    status     = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    reject_reason = models.TextField(blank=True)    # admin feedback on rejection

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reviewed_skill_courses",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "category"])]

    @property
    def price_rupees(self):
        return self.price // 100

    @property
    def is_free(self):
        return self.price == 0

    @property
    def section_count(self):
        return self.sections.count()

    @property
    def lecture_count(self):
        return SkillCourseLecture.objects.filter(section__course=self).count()

    def __str__(self):
        return f"{self.title} ({self.status})"


class SkillCourseSection(models.Model):
    id       = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course   = models.ForeignKey(SkillCourse, on_delete=models.CASCADE, related_name="sections")
    title    = models.CharField(max_length=200)
    order    = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.course.title} › {self.title}"


class SkillCourseLecture(models.Model):
    TYPE_VIDEO   = "video"
    TYPE_TEXT    = "text"
    TYPE_QUIZ    = "quiz"
    TYPE_CHOICES = [
        (TYPE_VIDEO, "Video"),
        (TYPE_TEXT,  "Text / article"),
        (TYPE_QUIZ,  "Quiz"),
    ]

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    section      = models.ForeignKey(SkillCourseSection, on_delete=models.CASCADE, related_name="lectures")
    title        = models.CharField(max_length=200)
    type         = models.CharField(max_length=8, choices=TYPE_CHOICES, default=TYPE_VIDEO)
    order        = models.PositiveIntegerField(default=0)
    video_url    = models.URLField(blank=True)
    content      = models.TextField(blank=True)   # markdown for text lectures
    duration_sec = models.PositiveIntegerField(default=0)
    is_preview   = models.BooleanField(default=False)  # free preview before purchase

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.section.course.title} › {self.section.title} › {self.title}"


class SkillCourseEnrollment(models.Model):
    """Per-LearnerProfile enrollment — one row per (learner, course) pair."""
    STATUS_ACTIVE    = "active"
    STATUS_COMPLETED = "completed"
    STATUS_EXPIRED   = "expired"
    STATUS_CHOICES   = [
        (STATUS_ACTIVE,    "Active"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_EXPIRED,   "Expired"),
    ]

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    learner_profile= models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE, related_name="skill_course_enrollments",
    )
    course         = models.ForeignKey(SkillCourse, on_delete=models.CASCADE, related_name="enrollments")
    status         = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    enrolled_at    = models.DateTimeField(auto_now_add=True)
    completed_at   = models.DateTimeField(null=True, blank=True)
    # payment reference
    payment_ref    = models.CharField(max_length=120, blank=True)
    amount_paid    = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("learner_profile", "course")]
        ordering = ["-enrolled_at"]

    def __str__(self):
        return f"{self.learner_profile} → {self.course.title}"


class SkillLectureProgress(models.Model):
    """Marks which lectures a learner has completed."""
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    enrollment  = models.ForeignKey(
        SkillCourseEnrollment, on_delete=models.CASCADE, related_name="progress"
    )
    lecture     = models.ForeignKey(SkillCourseLecture, on_delete=models.CASCADE)
    completed_at= models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("enrollment", "lecture")]
