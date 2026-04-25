from django.contrib import admin
from .models import Enrollment, Subscription


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("user", "course", "status", "enrolled_at")
    list_filter = ("status", "enrolled_at")
    search_fields = ("user__email", "course__title")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "course", "status", "starts_at", "expires_at")
    list_filter = ("status", "expires_at")
    search_fields = ("user__email", "course__title")
    autocomplete_fields = ("user", "course")
    raw_id_fields = ("source_request",)
    readonly_fields = ("created_at", "updated_at")
