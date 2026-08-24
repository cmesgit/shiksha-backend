"""
global_settings/serializers.py  (REPLACE THE WHOLE FILE)

Admin-facing serializer for the GlobalSettings singleton. Powers the payment-mode
switch (free / manual_upi / razorpay) plus UPI + Razorpay credentials.

FREE-LAUNCH SAFETY PIN
──────────────────────
The platform currently runs FREE. `manual_upi` and `razorpay` exist as
placeholders for later automation, but neither is wired end-to-end yet
(RazorpayProvider.create_intent raises NotImplementedError; no learner-facing
view creates SkillPaymentRequest). Until they are, this serializer REFUSES any
save whose *effective* mode would be a paid one — i.e. a paid `payment_mode`
combined with `free_trial_enabled = False`. An admin can still pre-select a
paid mode and pre-fill credentials while the free-trial switch stays ON.

To launch a paid mode later: implement it, then flip PAID_MODES_LIVE in
models.py (or delete the guard) — one line. It lives there, not here, because
GlobalSettings.effective_mode's fail-open-to-free trial-expiry logic depends
on the same live/not-live fact at read time, not just at save time.

Security (unchanged):
  * razorpay_key_secret is WRITE-ONLY — never returned in GET responses.
    On read we expose only `razorpay_secret_set` so the UI can show
    "configured" without leaking the value.
  * `effective_mode` (read-only) shows what's actually in force, since
    free_trial_enabled overrides payment_mode.
"""
from rest_framework import serializers

from .models import PAID_MODES_LIVE, GlobalSettings


class GlobalSettingsSerializer(serializers.ModelSerializer):
    # Never leak the secret back to the client.
    razorpay_key_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    razorpay_secret_set = serializers.SerializerMethodField()
    effective_mode = serializers.CharField(read_only=True)
    trial_ends_at = serializers.DateTimeField(read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)
    trial_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = GlobalSettings
        fields = [
            "payment_mode",
            "free_trial_enabled",
            "trial_started_at",
            "trial_duration_days",
            "trial_ends_at",           # read-only, computed
            "trial_days_remaining",    # read-only, computed
            "trial_active",            # read-only, computed
            "upi_id",
            "upi_payee_name",
            "razorpay_key_id",
            "razorpay_key_secret",   # write-only
            "razorpay_secret_set",   # read-only flag
            "platform_email",
            "skill_intro_session_paise",
            "skill_bundle_discount_pct",
            "effective_mode",        # read-only, computed
            # ── Live sessions ──────────────────────────────────────────
            "live_free_minutes_per_join",
            "live_max_participants",
            "live_max_session_minutes",
            "live_daily_minutes_per_user",
            "live_host_extensions_allowed",
            "live_host_extension_minutes",
            "live_max_upload_mb",
            "live_max_files_per_session",
            "live_file_retention_days",
            "live_recording_enabled",
            "live_remote_access_enabled",
            "live_chat_enabled",
            "live_screenshare_enabled",
            "live_show_first_visit_tour",
            "live_host_policy",
            "live_launch_free_mode",
            "tours_enabled",
            "quiz_v2_enabled",
            "ai_question_drafting_enabled",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def get_razorpay_secret_set(self, obj):
        return bool(obj.razorpay_key_secret)

    def validate(self, attrs):
        # Determine the resulting state after this patch.
        mode = attrs.get("payment_mode", getattr(self.instance, "payment_mode", None))
        free = attrs.get(
            "free_trial_enabled",
            getattr(self.instance, "free_trial_enabled", True),
        )

        # ── FREE-LAUNCH SAFETY PIN ────────────────────────────────────────
        # A paid mode may be selected/pre-configured, but it cannot GO LIVE
        # (free trial off) until it is implemented and flagged live above.
        if not free and mode in PAID_MODES_LIVE and not PAID_MODES_LIVE[mode]:
            label = dict(GlobalSettings.PAYMENT_CHOICES).get(mode, mode)
            raise serializers.ValidationError({
                "free_trial_enabled": (
                    f"'{label}' is not available yet — it is a placeholder for a "
                    "future payments launch. Keep the free-trial switch ON, or "
                    "select 'Free (no payment)'."
                )
            })

        # Only enforce credential presence when the mode would actually be LIVE
        # (free_trial off) — otherwise an admin can pre-fill config while still free.
        if not free:
            if mode == GlobalSettings.PAYMENT_MANUAL_UPI:
                upi = attrs.get("upi_id", getattr(self.instance, "upi_id", ""))
                if not upi:
                    raise serializers.ValidationError(
                        {"upi_id": "Required to go live with manual UPI."}
                    )
            if mode == GlobalSettings.PAYMENT_RAZORPAY:
                key_id = attrs.get(
                    "razorpay_key_id", getattr(self.instance, "razorpay_key_id", "")
                )
                secret_now = attrs.get("razorpay_key_secret", None)
                secret_existing = getattr(self.instance, "razorpay_key_secret", "")
                if not key_id or not (secret_now or secret_existing):
                    raise serializers.ValidationError(
                        {"razorpay_key_id":
                         "Razorpay key id + secret required to go live with Razorpay."}
                    )
        return attrs
