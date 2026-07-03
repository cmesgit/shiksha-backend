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
