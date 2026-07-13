# PLACEMENT: backend/backend/notifications/admin.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/admin.py

from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "recipient", "verb", "title", "audience_role", "is_read", "created_at")
    list_filter = ("verb", "audience_role", "is_read")
    search_fields = ("title", "body", "recipient__email", "actor__email")
    raw_id_fields = ("recipient", "actor")
    date_hierarchy = "created_at"


from .models import NotificationPreference, ReminderLog, SmsLog


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "email_enabled", "sms_enabled", "push_enabled",
                    "updated_at")
    search_fields = ("user__email",)
    raw_id_fields = ("user",)


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "status", "verb", "to", "phone_source",
                    "provider", "template_key", "error")
    list_filter = ("status", "provider", "verb")
    search_fields = ("to", "user__email", "error")
    raw_id_fields = ("user",)
    date_hierarchy = "created_at"


@admin.register(ReminderLog)
class ReminderLogAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "kind", "object_id", "offset_minutes")
    list_filter = ("kind", "offset_minutes")
