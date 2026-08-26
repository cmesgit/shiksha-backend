# PLACEMENT: backend/content/admin.py
#
# Django admin IS the CMS editing UI. Highlights:
#   • Publish / unpublish / feature bulk actions
#   • Status badges + live "View on site" links
#   • Path-style blog slugs auto-built from class/subject/chapter
#   • Optional rich-text editing: if `django-ckeditor-5` is installed and
#     configured, body fields upgrade automatically; otherwise a large
#     monospace textarea (fine for HTML fragments) is used.

from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem, HomeFloater,
    HomeContentBlock, HomeListItem, PublishStatus, ShowcaseCourse,
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
