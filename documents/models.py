# Explore document-library models.
#
# Mirrors the `forum` app's proven patterns: soft-delete moderation flags
# (is_removed/removed_at/is_locked), a generic Follow, a generic Report, an
# audit-log ModerationAction, and a per-user ban/suspend profile. The Explore-
# specific piece is the DuplicateFlag queue that powers the moderation panel's
# "Duplicate Review" section.

import hashlib

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


def compute_file_hash(uploaded_file):
    """SHA-256 of an uploaded file's bytes, as lowercase hex.

    Used to refuse a duplicate upload (the same person posting the same file
    twice). Reads in chunks so a 50 MB PDF never lands in memory whole, and
    rewinds the handle afterwards — Django has not saved the file yet at the
    point we hash it, so leaving the pointer at EOF would silently store a
    zero-byte document.
    """
    if not uploaded_file:
        return ""
    digest = hashlib.sha256()
    pos = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    try:
        uploaded_file.seek(0)
        for chunk in uploaded_file.chunks():
            digest.update(chunk)
    finally:
        uploaded_file.seek(pos)
    return digest.hexdigest()


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
    topic = models.CharField(max_length=120, blank=True, default="", db_default="")
    level = models.CharField(max_length=60, blank=True, default="")
    language = models.CharField(max_length=40, blank=True, default="English")
    institution = models.CharField(max_length=160, blank=True, default="")
    filetype = models.CharField(max_length=10, blank=True, default="PDF")
    file = models.FileField(upload_to=_document_file_path, null=True, blank=True)
    # SHA-256 of `file`, set on upload. Blank for the rows that predate this
    # field and for documents with no file at all; the uniqueness constraint
    # below excludes blanks so neither case blocks anything.
    file_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
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

    # ── Community verification ───────────────────────────────────────────
    # See documents/verification_models.py for the states and
    # documents/verification.py for the rules. Choices are referenced by
    # string here because verification_models imports Document — declaring
    # them inline would be a circular import.
    #
    # ⚠ Moderation (is_removed / is_locked) and verification are separate
    # axes and must stay that way. Moderation is a negative signal applied by
    # staff; verification is a positive one applied by peers. A verified set
    # can still be reported and removed, and a removed set keeps whatever
    # verification state it had so restoring it does not silently relaunch it
    # as unchecked.
    verification_status = models.CharField(
        max_length=20, default="unverified", db_index=True,
    )
    # Bumped whenever the files change. Reviews and invites key on it, which
    # is what makes "a new version restarts the count" true without deleting
    # the record of what earlier reviewers actually said.
    verification_version = models.PositiveIntegerField(default=1)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One copy of a given file per uploader. Two *different* people may
            # each upload the same public paper — that is legitimate, and is
            # what the moderation panel's Duplicate Review queue is for.
            #
            # The condition matters twice over: blank hashes (pre-existing rows,
            # and documents with no file) must not collide with each other, and
            # a soft-removed document must not permanently block its owner from
            # re-uploading the same file.
            models.UniqueConstraint(
                fields=["owner", "file_hash"],
                condition=models.Q(is_removed=False) & ~models.Q(file_hash=""),
                name="documents_unique_owner_file_hash",
            ),
        ]

    def __str__(self):
        return self.title


def _document_set_file_path(instance, filename):
    return f"explore/documents/{instance.document.owner_id}/{filename}"


class DocumentFile(models.Model):
    """One file inside a set.

    The Explore redesign uploads several files under one title — "Add one or
    more files as a set, then tag every topic they belong to." `Document` is
    that set; this is a file in it.

    ⚠ `Document` deliberately did NOT become a child of a new wrapper model.
    Nine things point at it — DocumentLike, SavedDocument, Collection's M2M,
    Follow, Report, ModerationAction, DuplicateFlag, the research provenance
    link and the public /explore/doc/<id> URL. Introducing a set *above*
    Document would have orphaned every one of them. Instead Document keeps its
    identity and grew a child, so every existing like, save, report and link
    still resolves.

    `Document.file` / `filetype` / `pages` / `file_hash` are still written and
    still hold the set's FIRST file. They are not dropped yet — see the
    deferred step in shiksha-backend/docs/EXPLORE_VERIFICATION_BUILD.md.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="files",
    )
    # Denormalised from document.owner / document.is_removed so the uniqueness
    # constraint in Meta can be enforced by the database — a UniqueConstraint
    # cannot reach through the FK. Kept in sync in save().
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="document_files",
    )
    is_removed = models.BooleanField(default=False)
    file = models.FileField(upload_to=_document_set_file_path)
    # The name the uploader's own filesystem used. `file.name` is the stored
    # path, which is not what the design shows in the Files list.
    original_name = models.CharField(max_length=255, blank=True, default="")
    filetype = models.CharField(max_length=10, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    pages = models.PositiveIntegerField(default=0)
    file_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            # The port of Document's `documents_unique_owner_file_hash`. That
            # constraint is what stops one person uploading the same file
            # twice; once a set can hold several files, the hash that matters
            # lives here, so the rule has to live here too.
            #
            # `owner` is denormalised onto the row for exactly this reason — a
            # UniqueConstraint cannot reach through the FK to Document.owner,
            # and without it the check would have to be a view-level query
            # that races.
            #
            # Same two exclusions as the original: blank hashes must not
            # collide with each other, and a soft-removed set must not block
            # its owner from re-uploading.
            models.UniqueConstraint(
                fields=["owner", "file_hash"],
                condition=models.Q(is_removed=False) & ~models.Q(file_hash=""),
                name="documents_unique_owner_set_file_hash",
            ),
        ]

    def save(self, *args, **kwargs):
        # Never let the denormalised pair drift from the parent: a stale
        # is_removed would make the duplicate constraint block a re-upload
        # forever, and a stale owner would block the wrong person.
        if self.document_id:
            self.owner_id = self.document.owner_id
            self.is_removed = self.document.is_removed
        super().save(*args, **kwargs)

    def __str__(self):
        return self.original_name or self.file.name


class DocumentTopic(models.Model):
    """One `field › subject › branch` path on a set — the design's "tag".

    A set carries several. The design's own sample is tagged both
    `Engineering › Civil Engineering › Water resources` and
    `Social Sciences › Sociology › Rural sociology`, and EITHER tag makes a
    specialist eligible to review it. That is the whole reason this is a table
    and not two more columns.

    `field` is stored rather than derived on read so that a subject's field can
    be remapped later without silently rewriting the tags on existing sets.
    `documents.fields.field_for_subject()` supplies it on write.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="topics",
    )
    field = models.CharField(max_length=60, blank=True, default="")
    subject = models.CharField(max_length=120)
    branch = models.CharField(max_length=120, blank=True, default="")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "subject", "branch"],
                name="documents_unique_document_topic",
            ),
        ]
        indexes = [
            # The reviewer-matching query is "every unverified set tagged with
            # a subject this person is confirmed in", so subject is the hot
            # column, not document.
            models.Index(fields=["subject"], name="doctopic_subject_idx"),
            models.Index(fields=["field"], name="doctopic_field_idx"),
        ]

    def __str__(self):
        parts = [p for p in (self.field, self.subject, self.branch) if p]
        return " › ".join(parts)


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


# Research submissions are deliberately separate from public Documents.
from .research_storage import ResearchPrivateStorage, research_file_path


class ResearchReviewer(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="research_reviewer")
    expertise = models.CharField(max_length=1000)
    subjects = models.JSONField(default=list)
    topics = models.JSONField(default=list, blank=True)
    approved = models.BooleanField(default=False)
    available = models.BooleanField(default=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="approved_research_reviewers")
    updated_at = models.DateTimeField(auto_now=True)


class ResearchSubmission(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Awaiting reviewer assignment"
        INVITED = "invited", "Waiting for reviewer acceptance"
        REVIEWING = "reviewing", "Under review"
        CHANGES = "changes", "Changes requested"
        APPROVAL = "approval", "Awaiting admin decision"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name="research_submissions")
    title = models.CharField(max_length=300)
    abstract = models.TextField()
    authors = models.CharField(max_length=1500)
    institution = models.CharField(max_length=160, blank=True)
    subject = models.CharField(max_length=120)
    topic = models.CharField(max_length=120, blank=True)
    original_url = models.URLField(blank=True, max_length=1000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    current_version = models.PositiveIntegerField(default=1)
    required_reviews = models.PositiveSmallIntegerField(default=1, db_default=1)
    automatic_matching = models.BooleanField(default=False, db_default=False)
    matching_notice_sent = models.BooleanField(default=False, db_default=False)
    published_version = models.PositiveIntegerField(null=True, blank=True)
    published_review_count = models.PositiveSmallIntegerField(default=1, db_default=1)
    document = models.OneToOneField(Document, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="research_submission")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]


class ResearchRevision(models.Model):
    submission = models.ForeignKey(ResearchSubmission, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    file = models.FileField(storage=ResearchPrivateStorage(), upload_to=research_file_path, max_length=250)
    file_hash = models.CharField(max_length=64)
    response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version"]
        constraints = [models.UniqueConstraint(fields=["submission", "version"], name="research_unique_revision")]


class ResearchAssignment(models.Model):
    submission = models.ForeignKey(ResearchSubmission, on_delete=models.CASCADE, related_name="assignments")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                 related_name="research_assignments")
    revision = models.ForeignKey(ResearchRevision, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default="invited")
    due_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    review_days = models.PositiveSmallIntegerField(default=7, db_default=7)
    reminded_at = models.DateTimeField(null=True, blank=True)
    decision = models.CharField(max_length=16, blank=True)
    feedback = models.TextField(blank=True)
    checks = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]
        constraints = [models.UniqueConstraint(fields=["submission", "reviewer", "revision"], condition=models.Q(active=True),
                                                name="research_unique_active_reviewer")]


class ResearchEvent(models.Model):
    submission = models.ForeignKey(ResearchSubmission, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=40)
    note = models.TextField(blank=True)
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class ResearchCase(models.Model):
    """Author appeal or proposed correction; public notices are explicit fields."""
    submission = models.ForeignKey(ResearchSubmission, on_delete=models.CASCADE, related_name="cases")
    kind = models.CharField(max_length=12, choices=[("appeal", "Appeal"), ("correction", "Correction")])
    version = models.PositiveIntegerField()
    reason = models.TextField()
    status = models.CharField(max_length=12, default="open")
    resolution = models.TextField(blank=True)
    public_note = models.TextField(blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "kind", "version"], name="research_unique_case_version"),
            models.UniqueConstraint(fields=["submission", "kind"], condition=models.Q(status="open"), name="research_one_open_case"),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Community verification
# ─────────────────────────────────────────────────────────────────────────────
# Defined in their own module to keep this file readable — it already holds the
# library, moderation and Research Hub models — but re-exported here so
# `from documents.models import VerificationReview` works like every other
# model in this app, and so makemigrations sees them.
#
# The import is at the BOTTOM on purpose: verification_models imports Document
# from this module, so anywhere earlier is a circular import.
from .verification_models import (  # noqa: E402,F401  (circular-import order)
    InviteState,
    ReviewOutcome,
    ReviewerSelection,
    SpecializationSource,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
    VerificationStatus,
)
