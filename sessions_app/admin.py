from django.contrib import admin
from .models import (
    PrivateSession,
    SessionParticipant,
    ChatMessage,
    StudyGroupSession,
    StudyGroupInvite,
)


# ---------------------------------------------------------
# Participant Inline (shows inside session)
# ---------------------------------------------------------
class SessionParticipantInline(admin.TabularInline):
    model = SessionParticipant
    extra = 0
    readonly_fields = ("user", "role", "joined_at", "left_at")
    can_delete = False


# ---------------------------------------------------------
# Private Session Admin
# ---------------------------------------------------------
@admin.register(PrivateSession)
class PrivateSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subject",
        "teacher",
        "requested_by",
        "status",
        "session_type",
        "scheduled_date",
        "scheduled_time",
        "started_at",
        "ended_at",
    )

    list_filter = (
        "status",
        "session_type",
        "scheduled_date",
    )

    search_fields = (
        "subject",
        "teacher__username",
        "teacher__email",
        "requested_by__username",
        "requested_by__email",
    )

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "started_at",
        "ended_at",
    )

    ordering = ("-created_at",)

    inlines = [SessionParticipantInline]

    # 🔥 FIX: Bulk delete (admin action)
    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj.delete()

    # 🔥 FIX: Single delete
    def delete_model(self, request, obj):
        obj.delete()


# ---------------------------------------------------------
# Participant Admin
# ---------------------------------------------------------
@admin.register(SessionParticipant)
class SessionParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "user",
        "role",
        "joined_at",
        "left_at",
    )

    list_filter = ("role",)

    search_fields = (
        "user__username",
        "user__email",
        "session__id",
    )

    readonly_fields = ("joined_at", "left_at")


# ---------------------------------------------------------
# Chat Message Admin
# ---------------------------------------------------------
@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "sender",
        "sender_role",
        "message_preview",
        "created_at",
    )

    list_filter = ("sender_role", "created_at")

    search_fields = (
        "sender__username",
        "message",
        "session__id",
    )

    readonly_fields = ("created_at",)

    ordering = ("-created_at",)

    def message_preview(self, obj):
        return obj.message[:50]

    message_preview.short_description = "Message"


# ---------------------------------------------------------
# Study Group admin
# ---------------------------------------------------------
class StudyGroupInviteInline(admin.TabularInline):
    model = StudyGroupInvite
    extra = 0
    readonly_fields = (
        "user", "invite_role", "status", "decline_count",
        "responded_at", "reinvited_at", "joined_at", "created_at",
    )
    can_delete = False


@admin.register(StudyGroupSession)
class StudyGroupSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "subject_name", "host", "status",
        "scheduled_date", "scheduled_time", "duration_minutes",
        "room_started_at", "ended_at",
    )
    list_filter = ("status", "duration_minutes", "scheduled_date")
    search_fields = (
        "subject_name", "course_title", "topic",
        "host__username", "host__email",
    )
    readonly_fields = (
        "id", "created_at", "updated_at", "room_started_at", "ended_at",
        "active_connections", "all_left_at", "room_name",
    )
    ordering = ("-created_at",)
    inlines = [StudyGroupInviteInline]


@admin.register(StudyGroupInvite)
class StudyGroupInviteAdmin(admin.ModelAdmin):
    list_display = (
        "id", "session", "user", "invite_role", "status",
        "decline_count", "responded_at",
    )
    list_filter = ("status", "invite_role")
    search_fields = (
        "user__username", "user__email", "session__id",
    )
    readonly_fields = (
        "created_at", "responded_at", "reinvited_at", "joined_at",
    )
