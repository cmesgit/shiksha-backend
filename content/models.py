# PLACEMENT: backend/content/models.py
#
# Editorial content models for ShikshaCom. Design goals:
#   • Draft → scheduled → published workflow on everything public-facing
#     (a single `status` + `publish_at` pair; `.published()` is the only
#     queryset the public API ever sees).
#   • Path-style blog slugs (`class-9/economics/chapter-1`) — identical to
#     the slugs the frontend already routes on, so every existing
#     /blogs/<slug> link keeps working when a post moves into the CMS.
#   • HTML bodies sanitized on save (defense-in-depth; see sanitize.py).
#     Fragments imported from the legacy extractor can be marked
#     `trusted_html=True` by staff to skip sanitization.
#   • Everything indexed the way the public API queries it.

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from .sanitize import clean_html

# ─────────────────────────────────────────────────────────────────
#  Shared bits
# ─────────────────────────────────────────────────────────────────

path_slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$",
    message=(
        "Use lowercase letters, numbers and hyphens; separate path "
        "segments with single slashes (e.g. class-9/economics/chapter-1)."
    ),
)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublishStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class PublishableQuerySet(models.QuerySet):
    def published(self):
        """Everything the anonymous public may see, in one place."""
        return self.filter(
            status=PublishStatus.PUBLISHED,
            publish_at__lte=timezone.now(),
        )


class PublishableModel(TimeStampedModel):
    """Status + scheduled publish time. `publish_at` in the future makes a
    'published' row invisible until the clock passes it — that is the whole
    scheduling mechanism, no cron needed."""

    status = models.CharField(
        max_length=12,
        choices=PublishStatus.choices,
        default=PublishStatus.DRAFT,
        db_index=True,
    )
    publish_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Set in the future to schedule publication.",
    )

    objects = PublishableQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def is_live(self):
        return (
            self.status == PublishStatus.PUBLISHED
            and self.publish_at <= timezone.now()
        )


class ContentTag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────
#  Blog posts (study chapters + editorial articles)
# ─────────────────────────────────────────────────────────────────

class ClassLevel(models.TextChoices):
    CLASS_8 = "8", "Class 8"
    CLASS_9 = "9", "Class 9"
    CLASS_10 = "10", "Class 10"
    CLASS_11 = "11", "Class 11"
    CLASS_12 = "12", "Class 12"
    GENERAL = "general", "General / not class-specific"


class Subject(models.TextChoices):
    SCIENCE = "science", "Science"
    MATHEMATICS = "mathematics", "Mathematics"
    HISTORY = "history", "History"
    GEOGRAPHY = "geography", "Geography"
    ECONOMICS = "economics", "Economics"
    CIVICS = "civics", "Civics"
    POLITICAL_SCIENCE = "political-science", "Political Science"
    ENGLISH = "english", "English"
    GENERAL = "general", "General"


class BlogPost(PublishableModel):
    title = models.CharField(max_length=300)
    slug = models.CharField(
        max_length=220,
        unique=True,
        blank=True,
        validators=[path_slug_validator],
        help_text=(
            "Path-style, e.g. class-9/economics/chapter-1. "
            "Left blank → built from class / subject / chapter."
        ),
    )
    class_level = models.CharField(
        max_length=10, choices=ClassLevel.choices,
        default=ClassLevel.GENERAL, db_index=True,
    )
    subject = models.CharField(
        max_length=24, choices=Subject.choices,
        default=Subject.GENERAL, db_index=True,
    )
    chapter_number = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="For chapter-wise study posts; leave empty for articles.",
    )

    excerpt = models.TextField(
        blank=True, default="",
        help_text="Short summary shown on listing cards (≈1–2 sentences).",
    )
    cover = models.ImageField(
        upload_to="content/blog/%Y/%m/", blank=True, null=True,
        help_text="Card thumbnail. ~800×450 recommended.",
    )
    body_html = models.TextField(
        help_text="Chapter/article body as HTML. Sanitized on save unless "
                  "'trusted html' is ticked.",
    )
    trusted_html = models.BooleanField(
        default=False,
        help_text="Skip HTML sanitization — only for first-party fragments "
                  "imported from the legacy extractor.",
    )

    tags = models.ManyToManyField(ContentTag, blank=True, related_name="blog_posts")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="content_blog_posts",
    )
    is_featured = models.BooleanField(default=False, db_index=True)

    # SEO / derived
    seo_title = models.CharField(max_length=70, blank=True, default="")
    seo_description = models.CharField(max_length=170, blank=True, default="")
    reading_minutes = models.PositiveSmallIntegerField(default=1, editable=False)
    view_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["-publish_at"]
        indexes = [
            models.Index(fields=["status", "publish_at"], name="content_blog_live_idx"),
            models.Index(fields=["class_level", "subject"], name="content_blog_taxo_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["class_level", "subject", "chapter_number"],
                condition=Q(chapter_number__isnull=False),
                name="content_blog_unique_chapter",
            ),
        ]

    # ── behaviour ──
    def _default_slug(self):
        parts = []
        if self.class_level and self.class_level != ClassLevel.GENERAL:
            parts.append(f"class-{self.class_level}")
        if self.subject and self.subject != Subject.GENERAL:
            parts.append(self.subject)
        if self.chapter_number:
            parts.append(f"chapter-{self.chapter_number}")
        else:
            parts.append(slugify(self.title)[:80] or "post")
        return "/".join(parts)

    def clean(self):
        super().clean()
        if self.slug:
            self.slug = self.slug.strip().strip("/").lower()
            path_slug_validator(self.slug)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._default_slug()
        if not self.trusted_html:
            self.body_html = clean_html(self.body_html)
        self.reading_minutes = max(
            1, round(len(re.sub(r"<[^>]+>", " ", self.body_html).split()) / 200)
        )
        if not self.seo_title:
            self.seo_title = self.title[:70]
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return f"/blogs/{self.slug}"

    def __str__(self):
        return self.title


# ─────────────────────────────────────────────────────────────────
#  Current affairs
# ─────────────────────────────────────────────────────────────────

class AffairCategory(models.TextChoices):
    NATIONAL = "national", "National"
    INTERNATIONAL = "international", "International"
    ECONOMY = "economy", "Economy"
    POLITY = "polity", "Polity & Governance"
    SCI_TECH = "science-tech", "Science & Technology"
    ENVIRONMENT = "environment", "Environment"
    SPORTS = "sports", "Sports"
    AWARDS = "awards", "Awards & Persons"
    MISC = "misc", "Miscellaneous"


class CurrentAffair(PublishableModel):
    title = models.CharField(max_length=300)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    affair_date = models.DateField(
        db_index=True, default=timezone.localdate,
        help_text="The date the event/news belongs to (drives grouping).",
    )
    category = models.CharField(
        max_length=20, choices=AffairCategory.choices,
        default=AffairCategory.NATIONAL, db_index=True,
    )
    summary = models.TextField(
        blank=True, default="",
        help_text="1–3 sentence summary shown in the list.",
    )
    body_html = models.TextField(blank=True, default="")
    source_name = models.CharField(max_length=120, blank=True, default="")
    source_url = models.URLField(blank=True, default="")
    tags = models.ManyToManyField(ContentTag, blank=True, related_name="current_affairs")

    class Meta:
        ordering = ["-affair_date", "-publish_at"]
        indexes = [
            models.Index(fields=["status", "publish_at"], name="content_ca_live_idx"),
            models.Index(fields=["affair_date", "category"], name="content_ca_date_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:180] or "affair"
            slug, n = base, 2
            while CurrentAffair.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        self.body_html = clean_html(self.body_html)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return f"/current-affairs#{self.slug}"

    def __str__(self):
        return f"{self.affair_date} · {self.title}"


# ─────────────────────────────────────────────────────────────────
#  FAQs (per page)
# ─────────────────────────────────────────────────────────────────

class FAQPage(models.TextChoices):
    HOME = "home", "Homepage"
    COURSES = "courses", "Courses"
    COUNSELLING = "counselling", "Counselling"
    SKILLS = "skills", "Skill Development"
    GENERAL = "general", "General / FAQ page"


class FAQItem(TimeStampedModel):
    page = models.CharField(
        max_length=20, choices=FAQPage.choices,
        default=FAQPage.GENERAL, db_index=True,
    )
    question = models.CharField(max_length=300)
    answer_html = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["page", "order", "id"]
        verbose_name = "FAQ item"

    def save(self, *args, **kwargs):
        self.answer_html = clean_html(self.answer_html)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_page_display()}] {self.question}"


# ─────────────────────────────────────────────────────────────────
#  Announcements (top strip / banners)
# ─────────────────────────────────────────────────────────────────

class AnnouncementLevel(models.TextChoices):
    INFO = "info", "Info"
    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"


class AnnouncementQuerySet(models.QuerySet):
    def live(self):
        now = timezone.now()
        return self.filter(is_active=True, starts_at__lte=now).filter(
            Q(ends_at__isnull=True) | Q(ends_at__gte=now)
        )


class Announcement(TimeStampedModel):
    message = models.CharField(max_length=300)
    link_url = models.CharField(
        max_length=300, blank=True, default="",
        help_text="Optional. Internal path (/courses) or full URL.",
    )
    link_label = models.CharField(max_length=60, blank=True, default="")
    level = models.CharField(
        max_length=10, choices=AnnouncementLevel.choices,
        default=AnnouncementLevel.INFO,
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(
        null=True, blank=True, help_text="Leave empty to run indefinitely.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    order = models.PositiveSmallIntegerField(default=0)

    objects = AnnouncementQuerySet.as_manager()

    class Meta:
        ordering = ["order", "-starts_at"]

    def clean(self):
        super().clean()
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Must be after the start time."})

    def __str__(self):
        return self.message


# ─────────────────────────────────────────────────────────────────
#  Homepage showcase cards ("Featured courses" grid)
# ─────────────────────────────────────────────────────────────────

class ShowcaseCourse(TimeStampedModel):
    """One card in the homepage 'Featured courses' grid. Field names mirror
    the frontend's homeData.js entries so the section can render straight
    from this endpoint."""

    title = models.CharField(max_length=120)
    level_label = models.CharField(
        max_length=40, help_text='Chip on the thumbnail, e.g. "Foundation".',
    )
    ribbon = models.CharField(
        max_length=20, blank=True, default="",
        help_text='Optional corner ribbon, e.g. "Bestseller".',
    )
    stars = models.PositiveSmallIntegerField(default=5)
    review_count = models.PositiveIntegerField(default=0)
    fact_line = models.CharField(
        max_length=80, default="1 Year · Online · Full access",
    )
    price_label = models.CharField(
        max_length=20, blank=True, default="",
        help_text='e.g. "1,500" (₹/month). Empty + tutor set = Coming Soon.',
    )
    tutor_name = models.CharField(max_length=80, blank=True, default="")
    is_explore_card = models.BooleanField(
        default=False, help_text="Render a single 'Explore Programs' button.",
    )
    categories = models.JSONField(
        default=list,
        help_text='Filter tabs this card appears in, e.g. ["class8-12"].',
    )
    gradient_css = models.CharField(
        max_length=160,
        default="rgba(15,157,107,0.72),rgba(11,91,62,0.88)",
        help_text="Two rgba() stops for the thumbnail overlay.",
    )
    image = models.ImageField(upload_to="content/showcase/", blank=True, null=True)
    image_url = models.URLField(
        blank=True, default="",
        help_text="Used if no image file is uploaded.",
    )
    icon = models.CharField(
        max_length=12, default="book",
        choices=[("book", "Book"), ("flask", "Flask"), ("calc", "Calculator")],
    )
    link_path = models.CharField(max_length=200, blank=True, default="/courses")
    link_state = models.JSONField(
        default=dict, blank=True,
        help_text='router state, e.g. {"selectedBoardGroup":"central",'
                  '"selectedBoard":"cbse"}',
    )
    course = models.ForeignKey(
        "courses.Course",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="showcase_cards",
        help_text="Optional link to a real course. When set, link_path/link_state "
                  "are derived server-side instead of the manual values.",
    )
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["order", "id"]

    def clean(self):
        super().clean()
        if self.stars > 5:
            raise ValidationError({"stars": "Maximum is 5."})
        if not isinstance(self.categories, list):
            raise ValidationError({"categories": "Must be a JSON list."})
        if self.course_id and self.is_explore_card:
            raise ValidationError({
                "is_explore_card": "A card linked to a real course can't also be a "
                                   "generic 'Explore Programs' card.",
            })

    def __str__(self):
        return self.title
