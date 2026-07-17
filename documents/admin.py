from django.contrib import admin

from .models import (
    DocTag, DocumentCategory, Document, DocumentLike, SavedDocument,
    Collection, Follow, DocumentProfile, Report, ModerationAction, DuplicateFlag,
)


@admin.register(DocumentCategory)
class DocumentCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "category", "filetype", "is_featured",
                    "is_trending", "is_removed", "created_at")
    list_filter = ("filetype", "is_featured", "is_trending", "is_removed", "is_locked")
    search_fields = ("title", "description", "owner__username", "owner__email")
    autocomplete_fields = ("owner", "category")
    filter_horizontal = ("tags",)


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "curator", "visibility", "created_at")
    list_filter = ("visibility",)
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("documents",)


@admin.register(DocumentProfile)
class DocumentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "is_banned", "suspended_until", "updated_at")
    list_filter = ("is_banned",)
    search_fields = ("user__username", "user__email")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("reporter", "reason", "content_type", "object_id", "resolved", "created_at")
    list_filter = ("reason", "resolved")


@admin.register(ModerationAction)
class ModerationActionAdmin(admin.ModelAdmin):
    list_display = ("action", "moderator", "target_user", "created_at")
    list_filter = ("action",)


@admin.register(DuplicateFlag)
class DuplicateFlagAdmin(admin.ModelAdmin):
    list_display = ("document", "original", "similarity", "status", "created_at")
    list_filter = ("status",)


admin.site.register(DocTag)
admin.site.register(DocumentLike)
admin.site.register(SavedDocument)
admin.site.register(Follow)
