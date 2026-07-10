from django.contrib import admin
from .models import Tag, ForumPost, Reply, PostUpvote, ReplyUpvote, ForumProfile


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(ForumPost)
class ForumPostAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "author", "is_solved", "view_count", "created_at")
    search_fields = ("title",)
    list_filter = ("created_at", "is_solved")


@admin.register(Reply)
class ReplyAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "author", "created_at")
    list_filter = ("created_at",)


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
