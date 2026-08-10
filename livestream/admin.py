from django.contrib import admin
from .models import (
    LiveSession,
    LiveSessionAttendance,
    LiveSessionAttendanceInterval,
    LiveKitWebhookEvent,
    LiveSessionViewerSample,
    StreamHealthSample,
    SessionReview,
    SessionNote,
)


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


@admin.register(LiveSessionAttendanceInterval)
class LiveSessionAttendanceIntervalAdmin(admin.ModelAdmin):
    list_display = ("session", "user", "joined_at", "left_at", "closed_by_reconcile")
    list_filter = ("closed_by_reconcile",)
    search_fields = ("user__email", "session__title")
    ordering = ("-joined_at",)
    readonly_fields = ("session", "user", "joined_at", "left_at", "closed_by_reconcile")


@admin.register(LiveKitWebhookEvent)
class LiveKitWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "room_name", "session", "processed", "received_at")
    list_filter = ("event_type", "processed")
    search_fields = ("event_id", "room_name", "session__title")
    ordering = ("-received_at",)
    readonly_fields = ("event_id", "event_type", "room_name", "session", "payload", "received_at", "processed", "error")


@admin.register(LiveSessionViewerSample)
class LiveSessionViewerSampleAdmin(admin.ModelAdmin):
    list_display = ("session", "viewers", "ts")
    search_fields = ("session__title",)
    ordering = ("-ts",)
    readonly_fields = ("session", "viewers", "ts")


@admin.register(StreamHealthSample)
class StreamHealthSampleAdmin(admin.ModelAdmin):
    list_display = ("session", "user", "is_presenter", "bitrate_kbps", "fps", "latency_ms", "quality", "ts")
    list_filter = ("is_presenter", "quality")
    search_fields = ("session__title", "user__email")
    ordering = ("-ts",)
    readonly_fields = ("session", "user", "is_presenter", "bitrate_kbps", "fps", "latency_ms", "packet_loss", "quality", "ts")


@admin.register(SessionReview)
class SessionReviewAdmin(admin.ModelAdmin):
    list_display = ("session", "user", "rating", "created_at")
    list_filter = ("rating",)
    search_fields = ("session__title", "user__email")
    ordering = ("-created_at",)
    readonly_fields = ("session", "user", "created_at")


@admin.register(SessionNote)
class SessionNoteAdmin(admin.ModelAdmin):
    list_display = ("session", "user", "updated_at")
    search_fields = ("session__title", "user__email")
    ordering = ("-updated_at",)
    readonly_fields = ("session", "user", "created_at", "updated_at")
