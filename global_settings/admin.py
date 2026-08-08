"""global_settings/admin.py — single-row settings editor in Django admin."""
from django.contrib import admin

from .models import GlobalSettings


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Payment mode", {
            "fields": ("free_trial_enabled", "trial_started_at", "trial_duration_days", "payment_mode"),
            "description": (
                "While <b>free trial</b> is on AND the countdown "
                "(trial_started_at + trial_duration_days) hasn't elapsed, the "
                "platform is free for everyone and the payment mode below is "
                "ignored. Turn the switch off, or let the countdown expire, "
                "to start charging using the selected mode — though it will "
                "only actually go live once that mode is implemented "
                "end-to-end (see PAID_MODES_LIVE in models.py)."
            ),
        }),
        ("Skill Dev pricing", {
            "fields": ("skill_intro_session_paise", "skill_bundle_discount_pct"),
            "description": "Informational while free-trial is active; used once it ends.",
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
