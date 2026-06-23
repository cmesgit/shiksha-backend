"""global_settings/admin.py — single-row settings editor in Django admin."""
from django.contrib import admin

from .models import GlobalSettings


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Payment mode", {
            "fields": ("free_trial_enabled", "payment_mode"),
            "description": (
                "While <b>free trial</b> is on, the platform is free for "
                "everyone and the payment mode below is ignored. Turn it off "
                "to start charging using the selected mode."
            ),
        }),
        ("Manual UPI", {
            "fields": ("upi_id", "upi_payee_name"),
            "description": "Used when payment mode is <b>Manual UPI</b>.",
        }),
        ("Razorpay", {
            "fields": ("razorpay_key_id", "razorpay_key_secret"),
            "description": "Used when payment mode is <b>Razorpay</b>.",
            "classes": ("collapse",),
        }),
        ("Platform", {"fields": ("platform_email",)}),
    )
    readonly_fields = ()

    def has_add_permission(self, request):
        # Only ever one row; once it exists, no "add" button.
        return not GlobalSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Jump straight to editing the single row instead of a list.
        from django.shortcuts import redirect
        from django.urls import reverse
        obj = GlobalSettings.load()
        return redirect(
            reverse("admin:global_settings_globalsettings_change", args=[obj.pk])
        )
