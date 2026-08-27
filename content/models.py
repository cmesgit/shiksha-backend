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
import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from .validators import validate_cms_image
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from .blocks import blocks_to_text
from .sanitize import clean_html, clean_html_restricted

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
    # design_handoff_content_studio Phase 1. The review step the Content Studio
    # workflow needs, added to the EXISTING vocabulary rather than as a second
    # one. The handoff spec asked for draft/review/live/hidden on a separate set
    # of models; that would have meant two status vocabularies in one app where
    # "published" and "live" mean the same thing and "archived" and "hidden"
    # mean the same thing — the same three-names-for-one-concept trap already
    # recorded for COACHING/competitive/Competitive. One vocabulary, four values.
    REVIEW = "review", "In review"
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


class StatusedContentModel(TimeStampedModel):
    """Draft/review state for the CMS rows that used to have only ``is_active``.

    design_handoff_content_studio Phases 1 and 9. ``is_active`` conflated "not
    finished yet" with "deliberately taken down", and the difference between
    those two is the entire review workflow. ``status`` splits them.

    The boolean is gone as of ``content/0024``. Between Phase 1 and Phase 9 it
    lived on as a real column kept in step by ``save()`` — deliberately a
    column and never a property, because ``content/admin.py`` used it in
    ``list_filter`` and ``list_editable``, and a property there raises
    ``admin.E116``/``admin.E121`` and stops the process booting. Everything now
    reads and writes ``status`` directly, so the compatibility layer went with
    the column.

    Subclasses keep their own ``objects`` manager — this base deliberately does
    NOT set one, because ``Announcement`` relies on ``AnnouncementQuerySet``.
    """

    status = models.CharField(
        max_length=12,
        choices=PublishStatus.choices,
        default=PublishStatus.PUBLISHED,
        db_index=True,
        help_text=(
            "Draft and In review are invisible to the public. Published is "
            "live. Archived is deliberately taken down."
        ),
    )

    class Meta:
        abstract = True

    @property
    def is_live(self):
        return self.status == PublishStatus.PUBLISHED


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


class Locale(models.TextChoices):
    EN = "en", "English"
    HI = "hi", "Hindi"


class BlogPost(PublishableModel):
    # A Hindi translation is a full second BlogPost row, not a shared field
    # on this one — publishing/scheduling/view-counts are genuinely
    # independent per locale in practice (a translator finishes days after
    # the English post ships, or an editor reworks the English text without
    # touching the live Hindi version). `translation_group` links "the same
    # post in different languages" without forcing a shared-identity/child-
    # translation-table rewrite of the entire publish/CRUD/cache pipeline
    # below — a Hindi post is just another BlogPost row with locale="hi".
    translation_group = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    locale = models.CharField(max_length=8, choices=Locale.choices, default=Locale.EN, db_index=True)

    title = models.CharField(max_length=300)
    slug = models.CharField(
        max_length=220,
        blank=True,
        validators=[path_slug_validator],
        help_text=(
            "Path-style, e.g. class-9/economics/chapter-1. "
            "Left blank → built from class / subject / chapter. "
            "Unique per locale, not globally — a Hindi translation "
            "conventionally reuses the same slug as its English sibling, "
            "disambiguated by the public URL's /hi/ prefix rather than by "
            "the slug string itself."
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
        validators=[validate_cms_image],
        help_text="Card thumbnail. ~800×450 recommended.",
    )
    body_html = models.TextField(
        help_text="Chapter/article body as HTML. Sanitized on save unless "
                  "'trusted html' is ticked. When body_blocks is non-empty "
                  "this becomes a DERIVED FALLBACK — computed and stored on "
                  "every save for legacy consumers and non-block-aware code "
                  "paths, but the public reader treats body_blocks as "
                  "authoritative and renders from it directly at read time. "
                  "See shared/src/blogBlocks/render.js.",
    )
    body_blocks = models.JSONField(
        default=list, blank=True,
        help_text="Block-tree body (shared/src/blogBlocks/schema.js). When "
                  "non-empty, this is what the public reader actually "
                  "renders — never body_html. Validated on write against "
                  "content.blocks.KNOWN_BLOCK_TYPES; permissive on read, so "
                  "a block type added by a newer frontend build simply "
                  "contributes no text to reading_minutes on an older one.",
    )
    body_theme = models.JSONField(
        default=dict, blank=True,
        help_text="Palette override for a block-authored post — a subset "
                  "of the 24 tokens in schema.js's THEME_TOKENS, each a hex "
                  "color. Travels as plain JSON and is injected into the "
                  "reader iframe as a <style> block by the frontend; never "
                  "passed through clean_html(), since the sanitizer's "
                  "ALLOWED_TAGS has no 'style' tag and would strip it.",
    )
    body_html_source = models.TextField(
        blank=True, default="", editable=False,
        help_text="Body as submitted, before sanitization. Kept so a "
                  "future sanitizer rule change can't destroy authored "
                  "content the way it silently did before this field existed.",
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
            models.Index(fields=["translation_group"], name="content_blog_transgroup_idx"),
            models.Index(fields=["locale", "status", "publish_at"], name="content_blog_locale_live_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["class_level", "subject", "chapter_number", "locale"],
                condition=Q(chapter_number__isnull=False),
                name="content_blog_unique_chapter_locale",
            ),
            models.UniqueConstraint(
                fields=["slug", "locale"],
                name="content_blog_unique_slug_locale",
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
            # The public /blogs/hi/<slug> route reserves "hi" (and any future
            # locale code) as the first path segment — a slug that starts
            # with it would collide with that prefix. Near-zero chance of
            # ever firing given how slugs are generated, but cheap to guard
            # once rather than discover in production.
            first_segment = self.slug.split("/", 1)[0]
            if first_segment in Locale.values:
                raise ValidationError(
                    {"slug": f"Slug can't start with the reserved locale segment \"{first_segment}\"."}
                )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._default_slug()
        self.body_html_source = self.body_html
        if not self.trusted_html:
            self.body_html = clean_html(self.body_html)
        # A block-authored post's word count comes from the blocks
        # themselves, not the derived body_html fallback — the two can
        # briefly disagree (e.g. a stylesheet-only change never touches
        # body_blocks) and blocks are the source of truth for a block post.
        if self.body_blocks:
            word_source = blocks_to_text(self.body_blocks)
        else:
            word_source = re.sub(r"<[^>]+>", " ", self.body_html)
        self.reading_minutes = max(1, round(len(word_source.split()) / 200))
        if not self.seo_title:
            self.seo_title = self.title[:70]
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        # Mirrors content/serializers.py's `_blog_path()` — English stays
        # unprefixed, every other locale gets a /blogs/<locale>/ segment.
        # Used by the sitemap (content/sitemaps.py), which would otherwise
        # point a Hindi row at the English route.
        if self.locale == Locale.EN:
            return f"/blogs/{self.slug}"
        return f"/blogs/{self.locale}/{self.slug}"

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


class FAQItem(StatusedContentModel):
    page = models.CharField(
        max_length=20, choices=FAQPage.choices,
        default=FAQPage.GENERAL, db_index=True,
    )
    question = models.CharField(max_length=300)
    answer_html = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)

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
        # Reads `status`. The `is_active` boolean this used to filter on was
        # dropped in content/0024.
        return self.filter(
            status=PublishStatus.PUBLISHED, starts_at__lte=now,
        ).filter(
            Q(ends_at__isnull=True) | Q(ends_at__gte=now)
        )


class Announcement(StatusedContentModel):
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

class ShowcaseCourse(StatusedContentModel):
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
    # NOTE: `stars` / `review_count` used to live here. They were never derived
    # from anything — no review model references Course or ShowcaseCourse — so
    # they rendered hand-typed numbers as if they were real social proof.
    # Removed in migration 0017. Reintroduce only as an aggregate over a real
    # review table, never as an editable column.
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
    # Keys must match shiksha-frontend's homeData.js COURSE_TABS ids. "all" is
    # deliberately excluded — it's a reserved sentinel FeaturedCourses.jsx
    # treats as "no filter applied", not a real category a card is tagged
    # with (see CATEGORY_CHOICES in Admin-dashboard's Showcase.jsx).
    CATEGORY_CHOICES = ("boards", "class8-12", "competitive")

    categories = models.JSONField(
        default=list,
        blank=True,
        help_text='Filter tabs this card appears in, e.g. ["class8-12"].',
    )
    gradient_css = models.CharField(
        max_length=160,
        default="rgba(15,157,107,0.72),rgba(11,91,62,0.88)",
        help_text="Two rgba() stops for the thumbnail overlay.",
    )
    image = models.ImageField(
        upload_to="content/showcase/", blank=True, null=True,
        validators=[validate_cms_image],
    )
    image_url = models.URLField(
        blank=True, default="",
        help_text="Used if no image file is uploaded.",
    )
    icon = models.CharField(
        max_length=12, default="book",
        # Keys must match shiksha-frontend's FeaturedCourses.jsx CAT_ICON_PATHS,
        # which already has SVGs for all of these — this choices list previously
        # only exposed 3 of the 7+ icons the public frontend could already render.
        choices=[
            ("book", "Book"), ("flask", "Flask"), ("calc", "Calculator"),
            ("compass", "Compass"), ("pulse", "Pulse"), ("target", "Target"),
            ("bank", "Bank"), ("shield", "Shield"), ("medal", "Medal"),
            ("institution", "Institution"),
        ],
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
    board = models.ForeignKey(
        "courses.Board",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="showcase_cards",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def clean(self):
        super().clean()
        if not isinstance(self.categories, list):
            raise ValidationError({"categories": "Must be a JSON list."})
        else:
            invalid = sorted(set(self.categories) - set(self.CATEGORY_CHOICES))
            if invalid:
                raise ValidationError({
                    "categories": f"Unknown categor{'y' if len(invalid) == 1 else 'ies'}: "
                                  f"{', '.join(invalid)}. Valid: {', '.join(self.CATEGORY_CHOICES)}.",
                })
        if self.course_id and self.is_explore_card:
            raise ValidationError({
                "is_explore_card": "A card linked to a real course can't also be a "
                                   "generic 'Explore Programs' card.",
            })

    def __str__(self):
        return self.title


# ─────────────────────────────────────────────────────────────────
#  Homepage content (Hero, WhyShiksha, TeachersStudents, BrowseCategories,
#  WhyChoose, Resources, Collaborate, Cta — everything on the public
#  homepage NOT already covered by ShowcaseCourse/FAQItem above)
# ─────────────────────────────────────────────────────────────────

class HomeSection(models.TextChoices):
    HERO = "hero", "Hero"
    WHY_SHIKSHA = "why_shiksha", "Why Shiksha"
    TEACHERS_STUDENTS = "teachers_students", "Teachers & Students"
    BROWSE_CATEGORIES = "browse_categories", "Browse Categories"
    # Rendered on the homepage (ShowcaseCourse-backed, see above) but has no
    # HomeContentBlock content of its own today — included here so it can
    # still take part in HomeSectionOrder's reorder/show-hide list below.
    FEATURED_COURSES = "featured_courses", "Featured Courses"
    WHY_CHOOSE = "why_choose", "Why Choose ShikshaCom"
    RESOURCES = "resources", "Resources & Support"
    COLLABORATE = "collaborate", "Collaborate"
    # Same as FEATURED_COURSES — FAQItem-backed, no HomeContentBlock of its
    # own, but still a real reorderable/hideable homepage section.
    FAQ = "faq", "FAQ"
    CTA = "cta", "Closing CTA"
    # /courses page (not the homepage) — reuses this same singleton-per-
    # section content-block table so its hero heading/copy/CTAs/illustration
    # are admin-editable through the identical HomeContentBlock pattern,
    # rather than inventing a second, courses-specific content model. Never
    # appears in HomeSectionOrder — that model is homepage-sequence only.
    COURSES_HERO = "courses_hero", "Courses Hero"
    # /about page — same reasoning as COURSES_HERO. Its five sections were
    # 100% hardcoded in About2.jsx (including a stray "DONT HACK US !!" left
    # in the hero badge), so nothing on that page could be corrected without
    # a frontend deploy. Prose lives in HomeContentBlock, the repeatable
    # bullets/pillars/cards in HomeListItem. Not homepage sections, so they
    # stay out of HOMEPAGE_SECTIONS and HomeSectionOrder.
    ABOUT_HERO = "about_hero", "About — Hero"
    ABOUT_VISION = "about_vision", "About — Our Vision"
    ABOUT_MISSION = "about_mission", "About — Our Mission"
    ABOUT_VALUES = "about_values", "About — Our Values"
    ABOUT_WHY = "about_why", "About — Why Choose ShikshaCom"
    # The /contact page, same story as the About sections above: its
    # heading, blurb and all four detail cards were hardcoded in
    # Contact.jsx, so correcting a phone number meant a code change and a
    # frontend deploy. One block for the header; the cards are list items
    # on this section, so an office or number can be added or removed
    # rather than being fixed at four.
    CONTACT_HERO = "contact_hero", "Contact — Header & details"


HOMEPAGE_SECTIONS = [
    HomeSection.HERO, HomeSection.WHY_SHIKSHA, HomeSection.TEACHERS_STUDENTS,
    HomeSection.BROWSE_CATEGORIES, HomeSection.FEATURED_COURSES,
    HomeSection.WHY_CHOOSE, HomeSection.RESOURCES, HomeSection.COLLABORATE,
    HomeSection.FAQ, HomeSection.CTA,
]  # excludes COURSES_HERO — matches ShikshaHome.jsx's current hardcoded
   # render order exactly; HomeSectionOrder's seed migration uses this list.


# Sections whose public component actually RENDERS HomeListItem rows.
#
# Verified one by one against shiksha-frontend: each of these destructures
# `items` from useHomeContent(section). The five that do not — HERO,
# FEATURED_COURSES, FAQ, CTA, COURSES_HERO — take only `block`, so a list item
# saved against them is invisible on the live site forever. Both CMS editors
# used to offer the list panel on every section regardless, so editors filled
# it in, saw it save, and nothing ever appeared.
#
# If a frontend section starts rendering `items`, add it here — this set is
# what the editors gate the panel on.
SECTIONS_WITH_LIST_ITEMS = frozenset({
    HomeSection.WHY_SHIKSHA,        # WhyShiksha.jsx
    HomeSection.TEACHERS_STUDENTS,  # TeachersStudents.jsx
    HomeSection.BROWSE_CATEGORIES,  # BrowseCategories.jsx
    HomeSection.WHY_CHOOSE,         # WhyChooseShiksha.jsx
    HomeSection.RESOURCES,          # Resources.jsx
    HomeSection.COLLABORATE,        # Collaborate.jsx
    HomeSection.CONTACT_HERO,       # Contact.jsx — the detail cards
    HomeSection.ABOUT_HERO,         # About2.jsx — stickers
    HomeSection.ABOUT_VISION,       # About2.jsx — bullets
    HomeSection.ABOUT_MISSION,      # About2.jsx — pillars
    HomeSection.ABOUT_VALUES,       # About2.jsx — core + digital
    HomeSection.ABOUT_WHY,          # About2.jsx — numbered
})


# Two of those five DO have repeatable content on the page — it just lives in
# another model, edited on another screen. Saying so beats silently hiding the
# panel and leaving the editor to wonder where the cards come from.
LIST_CONTENT_ELSEWHERE = {
    HomeSection.FEATURED_COURSES: {
        "label": "Course cards",
        "url": "/content/cards",
        "note": "The cards in this section come from your courses, not from a "
                "list here. Add or reorder them on the Course cards screen.",
    },
    HomeSection.FAQ: {
        "label": "Answers",
        "url": "/content/questions",
        "note": "The questions in this section come from your answers, not "
                "from a list here. Edit them on the Questions & notices screen.",
    },
}


class HomeContentBlock(StatusedContentModel):
    """One row per homepage section — its heading/copy/CTA/hero image.
    `section` is unique: this is a singleton-per-section table, not a list."""

    section = models.CharField(
        max_length=24, choices=HomeSection.choices, unique=True, db_index=True,
    )
    eyebrow = models.CharField(max_length=80, blank=True, default="")
    heading = models.CharField(max_length=200, blank=True, default="")
    heading_secondary = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Optional 2nd half of a two-part heading (only Hero uses this today).",
    )
    subhead = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")
    cta_primary_label = models.CharField(max_length=60, blank=True, default="")
    cta_primary_href = models.CharField(max_length=200, blank=True, default="")
    cta_secondary_label = models.CharField(max_length=60, blank=True, default="")
    cta_secondary_href = models.CharField(max_length=200, blank=True, default="")
    image = models.ImageField(
        upload_to="content/home/", blank=True, null=True,
        validators=[validate_cms_image],
    )
    image_url = models.URLField(
        blank=True, default="", help_text="Used if no image file is uploaded.",
    )
    extra = models.JSONField(
        default=dict, blank=True,
        help_text="Rare escape valve for a section-specific single value that "
                  "doesn't warrant its own column. Should normally be empty.",
    )

    class Meta:
        ordering = ["section"]
        verbose_name = "Homepage content block"

    def save(self, *args, **kwargs):
        self.body = clean_html_restricted(self.body)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_section_display()}] content block"


class HomeSectionOrder(TimeStampedModel):
    """One row per homepage section, controlling render sequence + whether
    it shows at all — independent of HomeContentBlock, which is about a
    section's copy/CTA. A section can be reordered or hidden even before any
    content block exists for it (e.g. FEATURED_COURSES/FAQ, which have no
    HomeContentBlock row at all)."""

    section = models.CharField(
        max_length=24, choices=HomeSection.choices, unique=True, db_index=True,
    )
    order = models.PositiveSmallIntegerField(default=0, db_index=True)
    is_visible = models.BooleanField(default=True)

    class Meta:
        ordering = ["order"]
        verbose_name = "Homepage section order"

    def __str__(self):
        return f"#{self.order} {self.get_section_display()}"


class HomeListVariant(models.TextChoices):
    DEFAULT = "default", "Default"
    MARQUEE_CHIP = "marquee_chip", "Marquee chip (Collaborate)"
    STAT_CHIP = "stat_chip", "Stat chip (Collaborate)"
    # /about page shapes. A section can hold more than one list (Values has
    # both "Our Core Values" and "Digital Mode of Learning"), so the variant
    # is what separates them rather than inventing extra sections.
    BULLET = "bullet", "Bullet (About — secondary list)"
    PILLAR = "pillar", "Pillar (About — Mission icon row)"
    NUMBERED = "numbered", "Numbered card (About — Why Choose)"
    # The About hero's row of small illustrations. Unlike every variant above
    # it carries no copy at all — the image *is* the content — so a row of
    # these is expected to have empty title/body.
    STICKER = "sticker", "Sticker (About — hero image row)"
    CONTACT_CARD = "contact_card", "Contact card (Contact — details)"


class HomeListItem(StatusedContentModel):
    """A repeatable card/chip within a homepage section (e.g. WhyShiksha's
    feature cards, BrowseCategories' category cards, Collaborate's marquee
    chips and stat chips — distinguished by `variant`)."""

    section = models.CharField(
        max_length=24, choices=HomeSection.choices, db_index=True,
    )
    variant = models.CharField(
        max_length=20, choices=HomeListVariant.choices, default=HomeListVariant.DEFAULT,
    )
    icon = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Icon key from the frontend's shared icon set.",
    )
    title = models.CharField(max_length=120, blank=True, default="")
    subtitle = models.CharField(max_length=160, blank=True, default="")
    body = models.TextField(blank=True, default="")
    pills = models.JSONField(
        default=list, blank=True,
        help_text="Short tag list, e.g. BrowseCategories' subject pills.",
    )
    stat_text = models.CharField(max_length=160, blank=True, default="")
    cta_label = models.CharField(max_length=60, blank=True, default="")
    cta_href = models.CharField(max_length=200, blank=True, default="")
    tint = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Design-token key (e.g. violet/green/gold) — the frontend "
                  "maps this to a CSS variable, never a raw color.",
    )
    # Same dual field/URL pair as HomeContentBlock, resolved to a single `img`
    # by the serializer. Added so per-card artwork (the About hero's sticker
    # row, optional art on the Why-Choose cards) is editable in the CMS —
    # before this, those images were hardcoded frontend imports and could only
    # be changed with a deploy.
    image = models.ImageField(
        upload_to="content/home/", blank=True, null=True,
        validators=[validate_cms_image],
    )
    image_url = models.URLField(
        blank=True, default="", help_text="Used if no image file is uploaded.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["section", "order", "id"]
        verbose_name = "Homepage list item"

    def clean(self):
        super().clean()
        if not isinstance(self.pills, list):
            raise ValidationError({"pills": "Must be a JSON list."})

    def save(self, *args, **kwargs):
        self.body = clean_html_restricted(self.body)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_section_display()}] {self.title or self.stat_text or self.pk}"


class HomeFloater(StatusedContentModel):
    """A decorative floating badge/icon anchored to one of a section's
    pre-tested CSS slots. `slot` is deliberately constrained per-section
    (see SLOT_CHOICES_BY_SECTION) and unique per section so two floaters can
    never be positioned on top of each other — there is no coordinate field
    for an editor to get wrong."""

    section = models.CharField(
        max_length=24, choices=HomeSection.choices, db_index=True,
    )
    slot = models.CharField(max_length=20)
    icon = models.CharField(max_length=40, blank=True, default="")
    label = models.CharField(max_length=60, blank=True, default="")
    sublabel = models.CharField(max_length=80, blank=True, default="")

    SLOT_CHOICES_BY_SECTION = {
        HomeSection.HERO: ["cap", "book", "play"],
        HomeSection.WHY_CHOOSE: ["b_tl", "b_tr", "b_bl"],
        HomeSection.COLLABORATE: ["top", "bottom"],
    }

    class Meta:
        ordering = ["section", "slot"]
        verbose_name = "Homepage floater"
        constraints = [
            models.UniqueConstraint(
                fields=["section", "slot"], name="content_homefloater_unique_slot",
            ),
        ]

    def clean(self):
        super().clean()
        allowed = self.SLOT_CHOICES_BY_SECTION.get(self.section, [])
        if self.slot not in allowed:
            allowed_text = allowed or "(none — this section has no floater slots)"
            raise ValidationError({
                "slot": f"'{self.slot}' is not a valid slot for section "
                        f"'{self.section}'. Allowed: {allowed_text}",
            })

    def __str__(self):
        return f"[{self.get_section_display()}] {self.slot}"


# ─────────────────────────────────────────────────────────────────
#  Blog revision history
# ─────────────────────────────────────────────────────────────────

class BlogRevision(models.Model):
    """A snapshot of a BlogPost's body taken right before an admin update
    overwrites it (see BlogPostAdminViewSet.perform_update). This is the real
    undo path for the block editor and its legacy-post importer —
    body_html_source is NOT a backup, since save() reassigns it from the
    incoming payload on every write, before sanitization runs; it is the
    pre-sanitizer copy of the CURRENT save, not the previous version.

    No pruning/retention policy — text fields, one row per edit, deliberately
    unbounded. A 53KB hand-authored chapter losing its history to a size cap
    would defeat the point."""
    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="revisions")
    body_html = models.TextField(blank=True, default="")
    body_blocks = models.JSONField(default=list, blank=True)
    body_theme = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    # Free text, e.g. "before legacy-HTML→blocks import" — optional context
    # for why this snapshot was taken, shown in the revision list.
    reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["post", "created_at"])]

    def __str__(self):
        return f"Revision of {self.post_id} @ {self.created_at:%Y-%m-%d %H:%M}"


# ─────────────────────────────────────────────────────────────────
#  Editor-uploaded images (rich-text body content, not a cover/logo)
# ─────────────────────────────────────────────────────────────────

class ContentImage(TimeStampedModel):
    """An image dropped into a rich-text editor body (blog/homepage). Not
    owned by any single BlogPost/HomeContentBlock — one post can embed
    several, and deleting the post shouldn't cascade-delete an image that
    might still be referenced elsewhere in its body_html_source history."""
    file = models.ImageField(
        upload_to="content/editor/%Y/%m/",
        validators=[validate_cms_image],
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    alt_text = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    # design_handoff_content_studio Phase 4. The filename as the person who
    # uploaded it knows it — `file.name` is the storage path, which is
    # timestamped and useless for recognising a picture in a grid.
    original_name = models.CharField(max_length=200, blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # Crop/thumbnail focal point as a fraction of image width/height
    # (0.0-1.0 each). No consumer reads these yet — forward-looking field
    # for a future smart-crop/thumbnail renderer; safe to leave null today.
    focal_x = models.FloatField(null=True, blank=True)
    focal_y = models.FloatField(null=True, blank=True)

    def __str__(self):
        return self.file.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.file and (self.width is None or self.height is None):
            self.width, self.height = self.file.width, self.file.height
            super().save(update_fields=["width", "height"])


# ─────────────────────────────────────────────────────────────────
#  Content Studio: revision history + per-author drafts
#  (design_handoff_content_studio Phase 1)
# ─────────────────────────────────────────────────────────────────


class ContentRevision(models.Model):
    """A snapshot of a CMS row taken immediately BEFORE a change is applied.

    Generic across every content model, which is what lets the History screen
    and its Undo work from one feed instead of one feed per table.

    ⚠ Distinct from ``BlogRevision`` above, deliberately. That one is
    blog-only, body-only, written from ``BlogPostAdminViewSet.perform_update``,
    and its docstring states its unbounded retention is intentional. Do not
    fold the two together: the block editor and its legacy-HTML importer both
    depend on ``BlogRevision``'s exact shape, and the pruning policy here would
    silently start deleting hand-authored chapter history.

    ⚠ Written by ``content.revisions.record_revision``, called explicitly from
    the admin views — NEVER from a ``post_save`` signal. A signal fires during
    migrations, during ``seed_content`` / ``_homepage_seed_data`` (~150 rows in
    one run), and on the public site's own writes, which would fill the History
    screen with entries no human caused.
    """

    RETENTION_PER_OBJECT = 50

    ACTION_CREATED = "created"
    ACTION_UPDATED = "updated"
    ACTION_PUBLISHED = "published"
    ACTION_HIDDEN = "hidden"
    ACTION_DELETED = "deleted"
    ACTION_RESTORED = "restored"
    ACTION_CHOICES = [
        (ACTION_CREATED, "Created"),
        (ACTION_UPDATED, "Updated"),
        (ACTION_PUBLISHED, "Published"),
        (ACTION_HIDDEN, "Hidden"),
        (ACTION_DELETED, "Deleted"),
        (ACTION_RESTORED, "Restored"),
    ]

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    # The row as it was BEFORE this change. Restore re-applies this.
    snapshot = models.JSONField(default=dict, blank=True)
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="content_revisions",
    )
    # Free text shown in the feed, e.g. "Restored the 12 Aug version".
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-created_at"]),
        ]
        verbose_name = "Content revision"

    def __str__(self):
        return f"{self.action} {self.content_type.model}#{self.object_id}"


class ContentDraft(models.Model):
    """Unpublished edits sitting on top of a live row.

    Keyed per (object, author) so two people editing the homepage at once do
    not silently overwrite each other — each sees their own pending changes
    and publishes them independently.

    ``payload`` holds ONLY the changed fields, not the whole row. That matters:
    publishing applies the payload onto whatever the row looks like at publish
    time, so a field someone else changed in the meantime survives instead of
    being reverted by a stale full-row copy.
    """

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    payload = models.JSONField(default=dict, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="content_drafts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "author"],
                name="uniq_contentdraft_object_author",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        ordering = ["-updated_at"]
        verbose_name = "Content draft"

    def __str__(self):
        who = self.author_id or "anonymous"
        return f"draft on {self.content_type.model}#{self.object_id} by {who}"

    @property
    def change_count(self):
        """Powers the '3 unpublished edits' chip in the publish bar."""
        return len(self.payload or {})


class MediaUsage(models.Model):
    """Where one library picture is actually used.

    design_handoff_content_studio Phase 4. This is the entire point of the
    Pictures screen: "used on 2 pages" is a fact nobody could establish before,
    because an image lived on the row that owned it and nothing recorded the
    reverse direction.

    It is also what makes deletion safe — a delete that would blank a live page
    is refused with the list of pages using it, rather than silently breaking
    them.

    Kept generic rather than a reverse FK per owner because the owners span
    four models today (``BlogPost.cover``, ``ShowcaseCourse.image``,
    ``HomeContentBlock.image``, ``HomeListItem.image``) and will span more.
    """

    asset = models.ForeignKey(
        ContentImage, on_delete=models.CASCADE, related_name="usages",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    # Which field on the owner points at this picture. Part of the key, so one
    # row using the same image in two fields counts as two usages.
    field_name = models.CharField(max_length=60)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "content_type", "object_id", "field_name"],
                name="uniq_mediausage_asset_target_field",
            ),
        ]
        indexes = [models.Index(fields=["content_type", "object_id"])]
        verbose_name = "Picture usage"

    def __str__(self):
        return f"{self.asset_id} on {self.content_type.model}#{self.object_id}.{self.field_name}"
