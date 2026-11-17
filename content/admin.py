# PLACEMENT: backend/content/admin.py
#
# Django admin IS the CMS editing UI. Highlights:
#   • Publish / unpublish / feature bulk actions
#   • Status badges + live "View on site" links
#   • Path-style blog slugs auto-built from class/subject/chapter
#   • Optional rich-text editing: if `django-ckeditor-5` is installed and
#     configured, body fields upgrade automatically; otherwise a large
#     monospace textarea (fine for HTML fragments) is used.

import json

from django import forms
from django.contrib import admin
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from . import demo_video_bunny
from .models import (
    Announcement, BlogPost, ContactMessage, ContentTag, CurrentAffair,
    DemoVideo, FAQItem, HomeFloater, HomeContentBlock, HomeListItem,
    NewsletterSubscriber, PublishStatus, ShowcaseCategory, ShowcaseCourse,
)

# ── optional rich-text widget ────────────────────────────────────
try:  # pragma: no cover - environment-dependent
    from django_ckeditor_5.widgets import CKEditor5Widget

    def body_widget():
        return CKEditor5Widget(config_name="default")
except ImportError:  # fallback: big monospace textarea
    def body_widget():
        return forms.Textarea(
            attrs={
                "rows": 28,
                "style": "font-family: ui-monospace, Menlo, monospace; "
                         "font-size: 13px; width: 95%;",
            }
        )


STATUS_COLORS = {
    PublishStatus.DRAFT: "#b45309",
    PublishStatus.REVIEW: "#2563eb",
    PublishStatus.PUBLISHED: "#0f9d6b",
    PublishStatus.ARCHIVED: "#6b7280",
}


class PublishableAdminMixin:
    actions = ["publish_now", "unpublish"]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        color = STATUS_COLORS.get(obj.status, "#6b7280")
        label = obj.get_status_display()
        if obj.status == PublishStatus.PUBLISHED and obj.publish_at > timezone.now():
            label, color = "Scheduled", "#7c5cfc"
        return format_html(
            '<span style="background:{}22;color:{};border:1px solid {}44;'
            'padding:2px 10px;border-radius:999px;font-weight:600;">{}</span>',
            color, color, color, label,
        )

    @admin.display(description="View")
    def view_link(self, obj):
        if not obj.is_live:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">Open ↗</a>',
            obj.get_absolute_url(),
        )

    @admin.action(description="Publish selected now")
    def publish_now(self, request, queryset):
        updated = queryset.update(
            status=PublishStatus.PUBLISHED, publish_at=timezone.now()
        )
        self.message_user(request, f"{updated} item(s) published.")

    @admin.action(description="Move selected back to draft")
    def unpublish(self, request, queryset):
        updated = queryset.update(status=PublishStatus.DRAFT)
        self.message_user(request, f"{updated} item(s) unpublished.")


# ── Blog ─────────────────────────────────────────────────────────

class BlogPostForm(forms.ModelForm):
    class Meta:
        model = BlogPost
        fields = "__all__"
        widgets = {"body_html": body_widget()}


@admin.register(BlogPost)
class BlogPostAdmin(PublishableAdminMixin, admin.ModelAdmin):
    form = BlogPostForm
    list_display = (
        "title", "class_level", "subject", "chapter_number",
        "status_badge", "publish_at", "is_featured", "view_count",
        "view_link",
    )
    list_filter = ("status", "class_level", "subject", "is_featured", "tags")
    search_fields = ("title", "slug", "excerpt")
    date_hierarchy = "publish_at"
    filter_horizontal = ("tags",)
    readonly_fields = ("reading_minutes", "view_count", "created_at", "updated_at")
    actions = PublishableAdminMixin.actions + ["feature", "unfeature"]
    list_per_page = 40
    save_on_top = True

    fieldsets = (
        (None, {"fields": ("title", "slug", "status", "publish_at")}),
        ("Placement", {
            "fields": ("class_level", "subject", "chapter_number",
                       "is_featured", "tags"),
        }),
        ("Content", {
            "fields": ("excerpt", "cover", "body_html", "trusted_html"),
            "description": (
                "Body is sanitized on save unless 'trusted html' is ticked "
                "(reserved for fragments imported from the legacy extractor)."
            ),
        }),
        ("SEO", {"classes": ("collapse",),
                 "fields": ("seo_title", "seo_description")}),
        ("Meta", {"classes": ("collapse",),
                  "fields": ("author", "reading_minutes", "view_count",
                             "created_at", "updated_at")}),
    )

    @admin.action(description="Mark as featured")
    def feature(self, request, queryset):
        queryset.update(is_featured=True)

    @admin.action(description="Remove featured flag")
    def unfeature(self, request, queryset):
        queryset.update(is_featured=False)

    def save_model(self, request, obj, form, change):
        if not change and not obj.author_id:
            obj.author = request.user
        super().save_model(request, obj, form, change)


# ── Current affairs ──────────────────────────────────────────────

class CurrentAffairForm(forms.ModelForm):
    class Meta:
        model = CurrentAffair
        fields = "__all__"
        widgets = {"body_html": body_widget()}


@admin.register(CurrentAffair)
class CurrentAffairAdmin(PublishableAdminMixin, admin.ModelAdmin):
    form = CurrentAffairForm
    list_display = ("title", "affair_date", "category", "status_badge",
                    "source_name", "view_link")
    list_filter = ("status", "category", "affair_date")
    search_fields = ("title", "summary", "slug")
    date_hierarchy = "affair_date"
    filter_horizontal = ("tags",)
    prepopulated_fields = {"slug": ("title",)}
    list_per_page = 50
    save_on_top = True


# ── FAQ / announcements / showcase / tags ────────────────────────

@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ("question", "page", "order", "status")
    list_filter = ("page", "status")
    list_editable = ("order", "status")
    search_fields = ("question", "answer_html")


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("message", "level", "starts_at", "ends_at",
                    "status", "live_now")
    list_filter = ("level", "status")
    list_editable = ("status",)
    search_fields = ("message",)

    @admin.display(boolean=True, description="Live now")
    def live_now(self, obj):
        now = timezone.now()
        return (obj.status == PublishStatus.PUBLISHED and obj.starts_at <= now
                and (obj.ends_at is None or obj.ends_at >= now))


@admin.register(ShowcaseCategory)
class ShowcaseCategoryAdmin(admin.ModelAdmin):
    """The Featured grid's filter tabs. `is_active` is a real field here,
    not a property — list_editable/list_filter against a non-field raise
    admin.E116 + E121 and stop the process booting."""

    list_display = ("label", "slug", "order", "is_active")
    list_editable = ("order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("slug", "label")
    prepopulated_fields = {"slug": ("label",)}


@admin.register(ShowcaseCourse)
class ShowcaseCourseAdmin(admin.ModelAdmin):
    list_display = ("title", "level_label", "ribbon", "price_label",
                    "order", "status")
    list_editable = ("order", "status")
    list_filter = ("status",)
    search_fields = ("title",)


@admin.register(ContentTag)
class ContentTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


# ── Homepage content ──────────────────────────────────────────────

@admin.register(HomeContentBlock)
class HomeContentBlockAdmin(admin.ModelAdmin):
    list_display = ("section", "heading", "status", "updated_at")
    list_filter = ("section", "status")
    search_fields = ("heading", "subhead", "body")


@admin.register(HomeListItem)
class HomeListItemAdmin(admin.ModelAdmin):
    list_display = ("section", "variant", "title", "order", "status")
    list_filter = ("section", "variant", "status")
    list_editable = ("order", "status")
    search_fields = ("title", "subtitle", "body")


@admin.register(HomeFloater)
class HomeFloaterAdmin(admin.ModelAdmin):
    list_display = ("section", "slot", "label", "status")
    list_filter = ("section", "status")
    search_fields = ("label", "sublabel")


def _json_body(request):
    """The request's JSON body as a dict, or an empty dict.

    Malformed JSON is not distinguished from an empty body on purpose: every
    field these endpoints read is validated individually anyway, so the caller
    gets "video_id required" rather than a parser error it cannot act on.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@admin.register(DemoVideo)
class DemoVideoAdmin(admin.ModelAdmin):
    """Where the two landing-page walkthroughs are configured.

    `duration_seconds` and `thumbnail_url` are read-only on purpose — they are
    owned by `manage.py sync_demo_videos`, and an editable field the next sync
    silently overwrites is worse than no field at all.

    ## Uploading

    The change form carries a file picker (see `demo_video_upload.js`) that
    sends the clip from this browser straight to Bunny and fills in the guid
    itself. The three endpoints behind it are wired in `get_urls` below.

    `bunny_video_id` stays an ordinary editable text field anyway. Pasting a
    guid from the Bunny dashboard was the only way to configure a row for the
    first six months of this feature's life, it still works, and it is the
    fallback when an upload fails for a reason nobody has met yet.

    Two things the upload deliberately does NOT do:

    * **It does not delete the clip it replaced.** Bunny keeps the old video
      and something else may still point at it; orphan cleanup is a separate
      job with separate consequences, not a side effect of picking a new file.
    * **It does not cap duration.** `skills` caps intro clips at 60s because
      those are user-submitted adverts on a public profile. These two clips are
      ours, and a walkthrough that needs 90 seconds should get 90 seconds.
    """

    list_display = ("title", "key", "order", "status", "has_video",
                    "on_the_site", "runtime")
    list_filter = ("status",)
    list_editable = ("order", "status")
    search_fields = ("title", "key", "blurb")
    readonly_fields = ("upload_panel", "duration_seconds", "thumbnail_url",
                       "bunny_status")
    fields = ("key", "title", "blurb", "order", "status", "upload_panel",
              "bunny_video_id", "bunny_status", "duration_seconds",
              "thumbnail_url")

    class Media:
        js = ("content/tus.min.js", "content/demo_video_upload.js")
        css = {"all": ("content/demo_video_upload.css",)}

    # ── the upload endpoints ─────────────────────────────────────

    def get_urls(self):
        """Three JSON endpoints, under this model's own admin URL space.

        Plain admin views rather than DRF: the only caller is the change form
        on the other side of `self.admin_site.admin_view`, so it already
        carries a session and a CSRF token, and routing it through DRF would
        mean a second authentication story for one page.

        Prepended, not appended — `ModelAdmin.get_urls` ends in a
        `<path:object_id>/` catch-all that would otherwise swallow these.
        """
        from django.urls import path

        mine = [
            path("<path:object_id>/upload-slot/",
                 self.admin_site.admin_view(self.upload_slot_view),
                 name="content_demovideo_upload_slot"),
            path("<path:object_id>/attach/",
                 self.admin_site.admin_view(self.attach_view),
                 name="content_demovideo_attach"),
            path("<path:object_id>/state/",
                 self.admin_site.admin_view(self.state_view),
                 name="content_demovideo_state"),
        ]
        return mine + super().get_urls()

    def _editable_or_404(self, request, object_id):
        """The row this request may change, or an error response.

        `admin_view` proves the caller is staff and nothing more. Change
        permission on *this model* is a separate question, and a read-only
        admin reaching the upload endpoint directly must not be able to
        repoint a homepage video.
        """
        obj = self.get_object(request, object_id)
        if obj is None:
            return None, JsonResponse({"error": "No such demo video."}, status=404)
        if not self.has_change_permission(request, obj):
            return None, JsonResponse(
                {"error": "You do not have permission to change this."}, status=403)
        return obj, None

    def _state(self, obj, *, reachable=True):
        """Everything the widget renders, derived in one place.

        The panel and the list columns must never disagree about whether a
        clip is live, so both read `on_the_site`.
        """
        return {
            "video_id": obj.bunny_video_id,
            "bunny_status": obj.bunny_status,
            "status_label": demo_video_bunny.status_label(obj.bunny_status),
            "duration_seconds": obj.duration_seconds,
            "runtime": self.runtime(obj),
            "thumbnail_url": obj.thumbnail_url,
            "embed_url": obj.embed_url(),
            "is_playable": obj.is_playable,
            "on_the_site": self.on_the_site(obj),
            "finished": obj.bunny_status == obj.BUNNY_FINISHED,
            # False means "Bunny did not answer", which is not the same as
            # "the clip is broken" — the widget says so rather than reporting
            # a state it did not actually learn.
            "reachable": reachable,
        }

    def upload_slot_view(self, request, object_id):
        """Mint an empty Bunny video and sign a ticket for it."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only."}, status=405)
        obj, err = self._editable_or_404(request, object_id)
        if err:
            return err

        payload = _json_body(request)
        # Bunny's dashboard shows this, so name it after the row rather than
        # leaving sixteen videos called "video".
        title = payload.get("filename") or f"{obj.key} — {obj.title}"
        try:
            ticket = demo_video_bunny.create_upload_slot(
                title, payload.get("size"))
        except demo_video_bunny.BunnyUnavailable as e:
            return JsonResponse({"error": str(e)}, status=502)
        return JsonResponse(ticket)

    def attach_view(self, request, object_id):
        """Point the row at an uploaded clip and ask Bunny how it went."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only."}, status=405)
        obj, err = self._editable_or_404(request, object_id)
        if err:
            return err

        video_id = (_json_body(request).get("video_id") or "").strip()
        if not video_id:
            return JsonResponse({"error": "video_id required."}, status=400)

        demo_video_bunny.attach(obj, video_id)
        # Bunny is almost never finished this early — the widget polls
        # `state/` from here. Syncing now is what records status 1/2 so the
        # panel can say "processing" instead of "not checked yet".
        _, data = demo_video_bunny.sync_demo_video(obj)
        self.log_change(request, obj, f"Uploaded a new clip ({video_id}).")
        return JsonResponse(self._state(obj, reachable=data is not None))

    def state_view(self, request, object_id):
        """Re-read Bunny's state for this row. Polled while a clip encodes."""
        obj, err = self._editable_or_404(request, object_id)
        if err:
            return err
        if not obj.bunny_video_id:
            return JsonResponse(self._state(obj))
        _, data = demo_video_bunny.sync_demo_video(obj)
        return JsonResponse(self._state(obj, reachable=data is not None))

    # ── the change-form panel ────────────────────────────────────

    @admin.display(description="Upload a clip")
    def upload_panel(self, obj):
        """The mount point the JS takes over.

        Rendered server-side so that with JS disabled — or if the vendored
        tus bundle ever fails to load — the form still explains what to do
        instead of showing an inert empty box.
        """
        if obj is None or not obj.pk:
            return format_html(
                '<p class="dv-hint">{}</p>',
                "Save this row first, then upload a clip.")
        state = self._state(obj)
        return format_html(
            '<div class="dv-upload" data-slot-url="{}" data-attach-url="{}" '
            'data-state-url="{}" data-state="{}">'
            '<p class="dv-hint">Choose a video file and it uploads straight to '
            "Bunny. The guid, runtime and thumbnail below fill in by "
            "themselves.</p></div>",
            reverse("admin:content_demovideo_upload_slot", args=[obj.pk]),
            reverse("admin:content_demovideo_attach", args=[obj.pk]),
            reverse("admin:content_demovideo_state", args=[obj.pk]),
            json.dumps(state),
        )

    @admin.display(boolean=True, description="Video uploaded")
    def has_video(self, obj):
        return bool(obj.bunny_video_id)

    @admin.display(description="On the site")
    def on_the_site(self, obj):
        """Why a row is or is not live, in words.

        "Published" plus a guid is not enough, and an editor who cannot see the
        reason will assume the site is broken — which is exactly what happened
        before this column existed.
        """
        if obj.status != PublishStatus.PUBLISHED:
            return "No — not published"
        if not obj.bunny_video_id:
            return "No — no video uploaded"
        if obj.bunny_status is None:
            # Reached from two places now — this column, and the upload panel
            # while a fresh clip encodes. "Run sync_demo_videos" was the only
            # answer before the panel existed and is now wrong half the time,
            # so say what is true in both: nobody has asked Bunny yet.
            return "No — not checked with Bunny yet"
        if obj.bunny_status != obj.BUNNY_FINISHED:
            return f"No — Bunny still at status {obj.bunny_status}"
        if not obj.embed_url():
            return "No — BUNNY_LIBRARY_ID unset"
        return "Yes"

    @admin.display(description="Runtime")
    def runtime(self, obj):
        if not obj.duration_seconds:
            # Bunny reports length 0 until it has finished processing, so
            # "not synced yet" and "still transcoding" look the same here.
            return "— not known yet"
        return f"{obj.duration_seconds // 60}:{obj.duration_seconds % 60:02d}"


# ── Contact form inbox ────────────────────────────────────────────

@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    """Read the enquiry, triage it — never edit it.

    Every field the visitor supplied is read-only. This is a record of what
    somebody actually sent us; an admin who can retype it can no longer be
    sure what the original said, which is exactly the question you ask when an
    enquiry is disputed. Only `status` and `handled_note` — our own workflow
    state, not theirs — are writable.

    No add permission either: the only legitimate way a row appears here is
    through the public form.
    """

    list_display = ("created_at", "name", "email", "topic", "role", "status")
    list_filter = ("status", "topic", "role", "created_at")
    list_editable = ("status",)
    search_fields = ("name", "email", "phone", "message")
    date_hierarchy = "created_at"
    readonly_fields = ("name", "email", "phone", "role", "topic", "message",
                       "consented_at", "submitted_ip", "created_at")
    fieldsets = (
        ("The enquiry", {
            "fields": ("name", "email", "phone", "role", "topic", "message"),
        }),
        ("Our handling", {"fields": ("status", "handled_note")}),
        ("Record", {
            "classes": ("collapse",),
            "fields": ("consented_at", "submitted_ip", "created_at"),
            "description": "Consent timestamp and origin IP, kept for abuse "
                           "handling and to evidence the basis for replying.",
        }),
    )

    def has_add_permission(self, request):
        return False


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    """The list collected by the contact page's CTA band.

    ``unsubscribed_at`` is the only editable field: set it to remove someone,
    rather than deleting the row. A hard delete loses the evidence that they
    asked, and lets the same address be re-added by anyone typing it into the
    public box.
    """

    list_display = ("email", "created_at", "unsubscribed_at")
    list_filter = ("unsubscribed_at", "created_at")
    search_fields = ("email",)
    date_hierarchy = "created_at"
    readonly_fields = ("email", "submitted_ip", "created_at")

    def has_add_permission(self, request):
        return False
