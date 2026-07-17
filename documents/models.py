# Explore document-library models.
#
# Mirrors the `forum` app's proven patterns: soft-delete moderation flags
# (is_removed/removed_at/is_locked), a generic Follow, a generic Report, an
# audit-log ModerationAction, and a per-user ban/suspend profile. The Explore-
# specific piece is the DuplicateFlag queue that powers the moderation panel's
# "Duplicate Review" section.

from django.db import models
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.utils.text import slugify


class DocTag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class DocumentCategory(models.Model):
    """A browsable document-type tile (Research Papers, Books, Notes, …).
    DB-backed + moderator-managed; slug is the stable public key the frontend
    uses (/explore/browse?category=<slug>). Soft-deleted via is_active."""

    slug = models.SlugField(max_length=140, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    icon = models.CharField(max_length=8, blank=True, default="")   # emoji glyph
    color = models.CharField(max_length=9, blank=True, default="#125027")
    blurb = models.CharField(max_length=200, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name_plural = "document categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "category"
            candidate = base
            i = 2
            while DocumentCategory.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{i}"
                i += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def _document_file_path(instance, filename):
    return f"explore/documents/{instance.owner_id}/{filename}"


class Document(models.Model):
    """A single uploaded document (paper / book / notes / …)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")   # short blurb / abstract
    full = models.TextField(blank=True, default="")          # long body shown in reader
    category = models.ForeignKey(
        DocumentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    subject = models.CharField(max_length=120, blank=True, default="")
    level = models.CharField(max_length=60, blank=True, default="")
    language = models.CharField(max_length=40, blank=True, default="English")
    institution = models.CharField(max_length=160, blank=True, default="")
    filetype = models.CharField(max_length=10, blank=True, default="PDF")
    file = models.FileField(upload_to=_document_file_path, null=True, blank=True)
    pages = models.PositiveIntegerField(default=0)
    view_count = models.PositiveIntegerField(default=0)
    download_count = models.PositiveIntegerField(default=0)
    rating = models.FloatField(default=0)
    is_featured = models.BooleanField(default=False)
    is_trending = models.BooleanField(default=False)
    tags = models.ManyToManyField(DocTag, blank=True, related_name="documents")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Moderator-only state (parallels forum.ForumPost). Uploads go live
    # immediately; moderation soft-hides via is_removed so counts stay
    # consistent and a "restore" is possible.
    is_locked = models.BooleanField(default=False)
    is_removed = models.BooleanField(default=False)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class DocumentLike(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_likes")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "document")

    def __str__(self):
        return f"{self.user} liked {self.document}"


class SavedDocument(models.Model):
    """A bookmark — the signed-in user saved a document for later."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_documents")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="saved_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "document")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} saved {self.document}"


class Collection(models.Model):
    """A curated set of documents (Scribd-style "collection")."""

    VIS_PUBLIC = "public"
    VIS_PRIVATE = "private"
    VIS_CHOICES = [(VIS_PUBLIC, "Public"), (VIS_PRIVATE, "Private")]

    curator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curated_collections",
    )
    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True, default="")
    color = models.CharField(max_length=9, blank=True, default="#125027")
    visibility = models.CharField(max_length=8, choices=VIS_CHOICES, default=VIS_PUBLIC)
    documents = models.ManyToManyField(Document, blank=True, related_name="collections")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "collection"
            candidate = base
            i = 2
            while Collection.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{i}"
                i += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class Follow(models.Model):
    """Generic follow: target_type ∈ author/collection/category, target_key is
    the username / collection slug / category slug. One table, three follows."""

    TARGET_AUTHOR = "author"
    TARGET_COLLECTION = "collection"
    TARGET_CATEGORY = "category"
    TARGET_CHOICES = [
        (TARGET_AUTHOR, "Author"),
        (TARGET_COLLECTION, "Collection"),
        (TARGET_CATEGORY, "Category"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_follows")
    target_type = models.CharField(max_length=12, choices=TARGET_CHOICES)
    target_key = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "target_type", "target_key")
        indexes = [models.Index(fields=["target_type", "target_key"])]

    def __str__(self):
        return f"{self.user} follows {self.target_type}:{self.target_key}"


class DocumentProfile(models.Model):
    """Uploader-facing profile + the ban/suspend state the moderation panel's
    Uploader Management acts on (parallels forum.ForumProfile)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_profile")
    headline = models.CharField(max_length=160, blank=True, default="")
    institution = models.CharField(max_length=160, blank=True, default="")
    bio = models.CharField(max_length=280, blank=True, default="")
    is_banned = models.BooleanField(default=False)
    ban_reason = models.TextField(blank=True, default="")
    # Temporary suspension (lifts itself lazily in _ban_error, no cron needed).
    suspended_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Document profile for {self.user}"


class Report(models.Model):
    """A user report against a document. Generic target so the same table can
    cover a document (and, later, a collection or comment) uniformly."""

    REASON_CHOICES = [
        ("copyright", "Copyright infringement"),
        ("plagiarism", "Plagiarism"),
        ("misleading", "Misleading / misinformation"),
        ("inappropriate", "Inappropriate content"),
        ("low_quality", "Low quality"),
        ("duplicate", "Duplicate"),
        ("other", "Other"),
    ]

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_reports")
    # related_name="+" — no reverse accessor (avoids clashing with forum.Report
    # on the shared ContentType model).
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    detail = models.TextField(blank=True, default="")
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["resolved"]),
        ]

    def __str__(self):
        return f"{self.reporter} reported {self.content_type} #{self.object_id} ({self.reason})"


class ModerationAction(models.Model):
    """Audit log for every Explore moderator action. Doubles as the source for
    the panel's Recent-actions feed and monthly analytics."""

    ACTION_DISMISS = "dismiss"
    ACTION_REMOVE = "remove"
    ACTION_WARN = "warn"
    ACTION_BAN = "ban"
    ACTION_UNBAN = "unban"
    ACTION_RESTORE = "restore"
    ACTION_SUSPEND = "suspend"
    ACTION_LOCK = "lock"
    ACTION_UNLOCK = "unlock"
    ACTION_CHOICES = [
        (ACTION_DISMISS, "Dismiss"),
        (ACTION_REMOVE, "Remove"),
        (ACTION_WARN, "Warn"),
        (ACTION_BAN, "Ban"),
        (ACTION_UNBAN, "Unban"),
        (ACTION_RESTORE, "Restore"),
        (ACTION_SUSPEND, "Suspend"),
        (ACTION_LOCK, "Lock"),
        (ACTION_UNLOCK, "Unlock"),
    ]

    moderator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="document_mod_actions")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="document_mod_actions_received")
    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    object_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["target_user"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} by {self.moderator} at {self.created_at}"


class DuplicateFlag(models.Model):
    """A document flagged as a likely duplicate of another, queued for the
    moderation panel's Duplicate Review section. Confirming it removes the
    duplicate; dismissing keeps it live."""

    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="duplicate_flags")
    original = models.ForeignKey(
        Document, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="duplicate_of_flags")
    similarity = models.PositiveIntegerField(default=0)   # 0-100 heuristic score
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="document_reviewed_duplicates")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"Duplicate flag for {self.document} ({self.status})"
