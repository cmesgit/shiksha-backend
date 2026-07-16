from django.contrib import admin
from .models import (
    Tag, ForumPost, Reply, PostUpvote, ReplyUpvote, ForumProfile,
    Space, SavedPost, Follow, Report, Attachment,
)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(ForumPost)
class ForumPostAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "kind", "author", "space", "is_solved", "view_count", "created_at")
    search_fields = ("title",)
    list_filter = ("kind", "created_at", "is_solved")


@admin.register(Reply)
class ReplyAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "kind", "author", "created_at")
    list_filter = ("kind", "created_at")


@admin.register(Space)
class SpaceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "topic", "creator", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SavedPost)
class SavedPostAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "post", "created_at")


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "target_type", "target_key", "created_at")
    list_filter = ("target_type",)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("id", "reporter", "content_type", "object_id", "reason", "resolved", "created_at")
    list_filter = ("reason", "resolved", "created_at")


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "kind", "original_name", "created_at")


@admin.register(PostUpvote)
class PostUpvoteAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "post", "created_at")


@admin.register(ReplyUpvote)
class ReplyUpvoteAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "reply", "created_at")


@admin.register(ForumProfile)
class ForumProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "updated_at")
    search_fields = ("user__username",)
