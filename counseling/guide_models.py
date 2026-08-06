# PLACEMENT: backend/backend/counseling/guide_models.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/guide_models.py
#
# The career-guidance content library — imported at the bottom of
# models.py, same pattern as skills/payment_models.py and
# skills/blackout_models.py: an additive module in an existing app
# rather than a new one.
#
# Lives in `counseling`, not `content`, on purpose: a guide needs to FK
# to Specialization so "Find counsellors for this ->" and
# services.match_counselors() can cross-link, and content -> counseling
# would invert the dependency direction (counseling already imports
# accounts/notifications; nothing imports counseling except
# notifications/tasks.py). What IS reused from content, by import, is
# the editorial machinery itself: PublishableModel (status + publish_at
# + .published(), no extra table), ContentTag, and clean_html — this is
# not a second parallel CMS.
#
# Sections are rows, not one JSON blob on the guide. The canonical
# Study-in-India source is 1.9M characters across 337 tables; per-section
# rows let the API serve one chapter at a time, let an editor fix one
# section without rewriting a megabyte of JSON, and give every section a
# stable id to deep-link into.

from django.db import models
from django.utils.text import slugify

from content.models import ContentTag, PublishableModel

from .models import Specialization


class CareerGuide(PublishableModel):
    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=300)
    blurb = models.CharField(max_length=300, blank=True, default="")

    # Free-text label shown on the card ("After Class 12 - Science") plus
    # the machine-comparable facet the frontend groups/filters on. Kept
    # separate on purpose: LibraryPage's filter chips today derive from
    # string-splitting `audience`, which breaks the moment the label is
    # reworded. `stage`/`stage_label`/`stage_order` are the fix.
    audience = models.CharField(max_length=120)
    stage = models.SlugField(max_length=24, db_index=True)
    stage_label = models.CharField(max_length=40)
    stage_order = models.PositiveSmallIntegerField(default=0)

    ACCENT_CHOICES = [("orange", "Orange"), ("green", "Green"), ("teal", "Teal")]
    accent = models.CharField(max_length=12, choices=ACCENT_CHOICES, default="teal")
    cover = models.ImageField(upload_to="counseling/guides/", null=True, blank=True)

    # Aliases a slug used to publish under, so a URL already shared /
    # bookmarked / linked from the navbar keeps resolving after a rename
    # (secondary-school replacing the old class-10 guide, for one).
    legacy_slugs = models.JSONField(default=list, blank=True)

    # At-a-glance spec table lifted out of the source document's body by
    # the importer — [[col1, col2], ...], header row first. Empty when
    # the source had no such table; an editor can still fill it in.
    glance = models.JSONField(default=list, blank=True)

    specializations = models.ManyToManyField(
        Specialization, blank=True, related_name="career_guides"
    )
    class_levels = models.JSONField(
        default=list, blank=True, help_text='e.g. ["9", "10"]',
    )
    tags = models.ManyToManyField(ContentTag, blank=True, related_name="career_guides")

    order = models.PositiveSmallIntegerField(default=0)
    seo_title = models.CharField(max_length=70, blank=True, default="")
    seo_description = models.CharField(max_length=170, blank=True, default="")
    reading_minutes = models.PositiveSmallIntegerField(default=1, editable=False)
    view_count = models.PositiveIntegerField(default=0, editable=False)

    # Import provenance, so re-running the importer is idempotent and
    # --replace can tell "unchanged, skip" from "source edited, rebuild".
    source_filename = models.CharField(max_length=200, blank=True, default="")
    source_sha256 = models.CharField(max_length=64, blank=True, default="")
    import_version = models.PositiveSmallIntegerField(default=0)
    imported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "title"]
        indexes = [models.Index(fields=["status", "publish_at"])]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return f"/counselling/guides/{self.slug}"


class GuideChapter(models.Model):
    guide = models.ForeignKey(CareerGuide, on_delete=models.CASCADE, related_name="chapters")
    slug = models.SlugField(max_length=120)
    number = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)

    # For the "For parents" / "Worksheets" tabs the frontend groups
    # chapters by — set from the importer's kind classifiers, editable
    # afterwards without touching the block content.
    KIND_CHOICES = [
        ("content", "Content"),
        ("worksheet", "Worksheet"),
        ("action_plan", "Action plan"),
        ("parent_guide", "Parent & guardian guide"),
        ("faq", "FAQ"),
        ("references", "References"),
    ]
    kind = models.CharField(max_length=14, choices=KIND_CHOICES, default="content")

    class Meta:
        unique_together = [("guide", "slug")]
        ordering = ["order", "number"]

    def __str__(self):
        return f"{self.guide.slug} · {self.title}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title) or f"chapter-{self.number}"
        super().save(*args, **kwargs)


class GuideSection(models.Model):
    guide = models.ForeignKey(CareerGuide, on_delete=models.CASCADE, related_name="sections")
    chapter = models.ForeignKey(
        GuideChapter, on_delete=models.CASCADE, null=True, blank=True, related_name="sections"
    )
    order = models.PositiveSmallIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=2, help_text="1=h1 2=h2 3=h3")
    title = models.CharField(max_length=300, blank=True, default="")
    anchor = models.SlugField(max_length=140, blank=True, default="")

    KIND_CHOICES = GuideChapter.KIND_CHOICES
    kind = models.CharField(max_length=14, choices=KIND_CHOICES, default="content", db_index=True)

    AUDIENCE_CHOICES = [
        ("student", "Student"),
        ("parent", "Parent / guardian"),
        ("teacher", "Teacher"),
    ]
    audience = models.CharField(
        max_length=8, choices=AUDIENCE_CHOICES, default="student", db_index=True
    )

    # The block tree — p | list | table | tip | ref | h3 | faq | worksheet
    # | checklist | steps | kv | note. Validated on write by
    # guide_serializers.validate_blocks(); unknown types simply render
    # null on old frontend builds so backend and frontend can ship out
    # of lockstep.
    blocks = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [models.Index(fields=["guide", "order"])]

    def __str__(self):
        return self.title or f"{self.guide.slug} section #{self.order}"

    def save(self, *args, **kwargs):
        # Blocks hold PLAIN TEXT, not HTML — GuidePage.jsx renders block
        # strings as JSX children (`<p>{b.text}</p>`), which already
        # escapes markup on the way to the DOM, and nothing here ever
        # uses dangerouslySetInnerHTML. Running content.sanitize.clean_html
        # (an HTML sanitizer) over plain prose would corrupt every "&" in
        # these documents into "&amp;" wherever nh3 is installed — it is
        # deliberately NOT called here. Unknown/unexpected block shapes
        # are rejected before they reach the database by
        # guide_serializers.validate_blocks() instead.
        if not self.anchor and self.title:
            self.anchor = slugify(self.title)[:140]
        super().save(*args, **kwargs)
