from django.contrib import admin
from .models import Enrollment, Subscription, EnrollmentRequest


@admin.register(EnrollmentRequest)
class EnrollmentRequestAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "course",
        "status",
        "amount_paid",
        "payment_method",
        "submitted_at",
        "reviewed_at",
    )
    list_filter = ("status", "payment_method", "submitted_at")
    search_fields = ("user__email", "course__title", "utr_number")
    autocomplete_fields = ("user", "course", "reviewed_by")
    readonly_fields = ("submitted_at",)
    ordering = ("-submitted_at",)


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("user", "course", "batch_code", "status", "enrolled_at")
    list_filter = ("status", "enrolled_at")
    search_fields = ("user__email", "course__title", "batch_code")
    autocomplete_fields = ("user", "course")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "course", "status", "starts_at", "expires_at")
    list_filter = ("status", "expires_at")
    search_fields = ("user__email", "course__title")
    autocomplete_fields = ("user", "course")
    raw_id_fields = ("source_request",)
    readonly_fields = ("created_at", "updated_at")
