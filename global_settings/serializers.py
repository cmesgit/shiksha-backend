"""
global_settings/serializers.py  (NEW FILE)

Admin-facing serializer for the GlobalSettings singleton. Powers the payment-mode
switch (free / manual_upi / razorpay) plus UPI + Razorpay credentials.

Security:
  * razorpay_key_secret is WRITE-ONLY — it is never returned in GET responses.
    On read we expose only whether a secret is set (`razorpay_secret_set`), so
    the admin UI can show "configured" without leaking the value.
  * `effective_mode` (read-only) shows what's actually in force right now, since
    free_trial_enabled overrides payment_mode.
"""
from rest_framework import serializers

from .models import GlobalSettings


class GlobalSettingsSerializer(serializers.ModelSerializer):
    # Never leak the secret back to the client.
    razorpay_key_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    razorpay_secret_set = serializers.SerializerMethodField()
    effective_mode = serializers.CharField(read_only=True)

    class Meta:
        model = GlobalSettings
        fields = [
            "payment_mode",
            "free_trial_enabled",
            "upi_id",
            "upi_payee_name",
            "razorpay_key_id",
            "razorpay_key_secret",   # write-only
            "razorpay_secret_set",   # read-only flag
            "platform_email",
            "effective_mode",        # read-only, computed
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
