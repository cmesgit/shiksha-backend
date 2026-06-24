from django.contrib import admin
from .models import LiveSession, LiveSessionAttendance


@admin.register(LiveSession)
class LiveSessionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "course",
        "subject",
        "created_by",
        "start_time",
        "end_time",
        "status",
    )

    list_filter = ("status", "course", "subject")
    search_fields = ("title", "room_name", "created_by__email")
    readonly_fields = ("room_name",)
    ordering = ("-start_time",)
    actions = ["mark_cancelled"]

    def mark_cancelled(self, request, queryset):
        queryset.update(status=LiveSession.STATUS_CANCELLED)

    mark_cancelled.short_description = "Mark selected sessions as Cancelled"


@admin.register(LiveSessionAttendance)
class LiveSessionAttendanceAdmin(admin.ModelAdmin):
    list_display = (
        "session",
        "user",
        "joined_at",
        "left_at",
        "duration",
    )

    list_filter = ("session",)
    search_fields = ("user__email", "session__title")
    ordering = ("-joined_at",)

    readonly_fields = (
        "session",
        "user",
        "joined_at",
        "left_at",
    )

    def duration(self, obj):
        if obj.joined_at and obj.left_at:
            return obj.left_at - obj.joined_at
        return "—"

    duration.short_description = "Duration"
