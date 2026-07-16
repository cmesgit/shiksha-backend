# PLACEMENT: backend/backend/forum/models.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/forum/models.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The Notification model is GONE. Notifications now live in the site-wide
# `notifications` app (one table for forum, counseling, assignments, ...).
# Existing rows are copied by notifications/0002 before forum/0005 drops
# the old table. Everything else in this file is byte-identical.

from django.db import models
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.utils.text import slugify


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Space(models.Model):
    """A community ("Space") that questions and posts can be filed under.
    Membership == following (see Follow with target_type="space")."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.TextField(blank=True, default="")
    initials = models.CharField(max_length=4, blank=True, default="")
    color = models.CharField(max_length=9, blank=True, default="#125027")
    topic = models.CharField(max_length=60, blank=True, default="")
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_spaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "space"
            candidate = base
            i = 2
            while Space.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{i}"
                i += 1
            self.slug = candidate
        if not self.initials:
            words = [w for w in "".join(
                c if (c.isalnum() or c.isspace()) else " " for c in self.name
            ).split() if w]
            self.initials = ("".join(w[0] for w in words[:2]) or "SP").upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ForumCategory(models.Model):
    """A curated, browsable category tile. DB-backed (moderator-managed)
    replacement for the old hardcoded FORUM_CATEGORIES list — slug is the
    public id, kept stable across edits so Follow.target_key and existing
    frontend links (/forum/category/<slug>) don't break."""

    slug = models.SlugField(max_length=140, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    initials = models.CharField(max_length=4, blank=True, default="")
    color = models.CharField(max_length=9, blank=True, default="#125027")
    topic = models.CharField(max_length=60, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name_plural = "forum categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "category"
            candidate = base
            i = 2
            while ForumCategory.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{i}"
                i += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ForumPost(models.Model):
    KIND_QUESTION = "question"
    KIND_POST = "post"
    KIND_CHOICES = [(KIND_QUESTION, "Question"), (KIND_POST, "Post")]

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_posts"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_QUESTION)
    space = models.ForeignKey(
        Space,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posts",
    )
    title = models.CharField(max_length=300)
    content = models.TextField(blank=True, default="")
    tags = models.ManyToManyField(Tag, blank=True, related_name="posts")
    view_count = models.PositiveIntegerField(default=0)
    is_solved = models.BooleanField(default=False)
    accepted_reply = models.ForeignKey(
        "Reply",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_for_post",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Moderator-only state. Regular self/staff delete (DeleteThreadView) is
    # still a hard delete — these two only apply to the moderator-panel-
    # initiated soft-delete/lock actions (forum/moderation_views.py).
    is_locked = models.BooleanField(default=False)
    is_removed = models.BooleanField(default=False)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class PostUpvote(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="post_upvotes"
    )
    post = models.ForeignKey(
        ForumPost,
        on_delete=models.CASCADE,
        related_name="upvotes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "post")

    def __str__(self):
        return f"{self.user} upvoted {self.post}"


class Reply(models.Model):
    KIND_ANSWER = "answer"
    KIND_COMMENT = "comment"
    KIND_CHOICES = [(KIND_ANSWER, "Answer"), (KIND_COMMENT, "Comment")]

    post = models.ForeignKey(
        ForumPost,
        on_delete=models.CASCADE,
        related_name="replies"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_ANSWER)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_replies"
    )
    reply_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children"
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "replies"

    def __str__(self):
        return f"Reply by {self.author} on {self.post}"


class ReplyUpvote(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reply_upvotes"
    )
    reply = models.ForeignKey(
        Reply,
        on_delete=models.CASCADE,
        related_name="upvotes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "reply")

    def __str__(self):
        return f"{self.user} upvoted reply on {self.reply.post}"


class ForumProfile(models.Model):
    """A small, forum-owned public profile: just a bio the person can set
    for themselves. Everything else shown on a profile page (thread count,
    reply count, upvotes received, member-since) is computed on read from
    existing forum data, not stored here."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_profile",
    )
    display_name = models.CharField(max_length=120, blank=True, default="")
    headline = models.CharField(max_length=160, blank=True, default="")
    location = models.CharField(max_length=120, blank=True, default="")
    bio = models.CharField(max_length=280, blank=True, default="")
    is_banned = models.BooleanField(default=False)
    ban_reason = models.TextField(blank=True, default="")
    # Temporary suspension — distinct from a permanent ban. Null/past =
    # not suspended. Lifts itself automatically once the timestamp passes
    # (checked lazily in _ban_error, no cron job needed).
    suspended_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Forum profile for {self.user}"


class SavedPost(models.Model):
    """A bookmark — the signed-in user saved a question/post for later."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_posts",
    )
    post = models.ForeignKey(
        ForumPost,
        on_delete=models.CASCADE,
        related_name="saved_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "post")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} saved {self.post}"


class Follow(models.Model):
    """A generic follow relationship. target_type is one of space/question/
    category; target_key is the space slug, the post id, or the category id.
    One table powers all three follows plus space member counts."""

    TARGET_SPACE = "space"
    TARGET_QUESTION = "question"
    TARGET_CATEGORY = "category"
    TARGET_CHOICES = [
        (TARGET_SPACE, "Space"),
        (TARGET_QUESTION, "Question"),
        (TARGET_CATEGORY, "Category"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_follows",
    )
    target_type = models.CharField(max_length=12, choices=TARGET_CHOICES)
    target_key = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "target_type", "target_key")
        indexes = [
            models.Index(fields=["target_type", "target_key"]),
        ]

    def __str__(self):
        return f"{self.user} follows {self.target_type}:{self.target_key}"


class Report(models.Model):
    """A user report against a question, answer or comment. Targets are
    referenced generically so one table covers every reportable object."""

    REASON_CHOICES = [
        ("spam", "Spam"),
        ("abusive", "Abusive content"),
        ("duplicate", "Duplicate"),
        ("misleading", "Misleading information"),
        ("other", "Other"),
    ]

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_reports",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
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


def _forum_attachment_path(instance, filename):
    return f"forum/attachments/{instance.post_id}/{filename}"


class Attachment(models.Model):
    """A file or image attached to a question/post at creation time."""

    KIND_IMAGE = "image"
    KIND_FILE = "file"
    KIND_CHOICES = [(KIND_IMAGE, "Image"), (KIND_FILE, "File")]

    post = models.ForeignKey(
        ForumPost,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to=_forum_attachment_path)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_FILE)
    original_name = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="forum_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.original_name or self.file.name


class ModerationAction(models.Model):
    """Audit log for every moderator action — dismiss/delete/warn/ban/unban/
    restore. Doubles as the source for the Moderator Panel's 'Recent actions'
    feed and monthly analytics counts, so nothing needs a separate counter."""

    ACTION_DISMISS = "dismiss"
    ACTION_DELETE = "delete"
    ACTION_WARN = "warn"
    ACTION_BAN = "ban"
    ACTION_UNBAN = "unban"
    ACTION_RESTORE = "restore"
    ACTION_SUSPEND = "suspend"
    ACTION_LOCK = "lock"
    ACTION_UNLOCK = "unlock"
    ACTION_CHOICES = [
        (ACTION_DISMISS, "Dismiss"),
        (ACTION_DELETE, "Delete"),
        (ACTION_WARN, "Warn"),
        (ACTION_BAN, "Ban"),
        (ACTION_UNBAN, "Unban"),
        (ACTION_RESTORE, "Restore"),
        (ACTION_SUSPEND, "Suspend"),
        (ACTION_LOCK, "Lock"),
        (ACTION_UNLOCK, "Unlock"),
    ]

    moderator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="forum_mod_actions",
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="forum_mod_actions_received",
    )
    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
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


class AutoRejectedSubmission(models.Model):
    """A question/post/answer/comment that the scanner flagged before it ever
    became a real ForumPost/Reply. Held here for moderator review; 'restore'
    creates the real object from these stored fields."""

    KIND_CHOICES = ForumPost.KIND_CHOICES + Reply.KIND_CHOICES

    STATUS_PENDING = "pending"
    STATUS_DELETED = "deleted"
    STATUS_RESTORED = "restored"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DELETED, "Deleted"),
        (STATUS_RESTORED, "Restored"),
    ]

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_auto_rejected",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    title = models.CharField(max_length=300, blank=True, default="")
    content = models.TextField(blank=True, default="")
    thread = models.ForeignKey(
        ForumPost,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auto_rejected_children",
    )
    tags = models.CharField(max_length=300, blank=True, default="")
    categories = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="forum_reviewed_submissions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Auto-rejected {self.kind} by {self.author} ({self.status})"
