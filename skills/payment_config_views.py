"""
skills/payment_config_views.py — payment configuration for the skill marketplace.

Route (wired in skills/urls.py, mounted under /api/skill/):
    GET /api/skill/payment-config/   → active payment mode + the payee details
                                       the booking / course-buy screens need.

Everything is read from GlobalSettings, so the admin can flip free ↔ manual UPI
↔ Razorpay live with no server restart. The frontend calls this on load and
decides whether to show a one-tap free enroll, the UPI form, or a gateway button.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from enrollments.payments import get_payment_provider


class SkillPaymentConfigView(APIView):
    """Active payment mode for the skill marketplace, plus payee details."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = get_payment_provider()
        data = provider.describe()

        # Only surface payee details for the mode that's actually live, and
        # never expose the Razorpay secret key.
        if provider.name == "manual_upi":
            gs = self._settings()
            data["upi"] = {
                "vpa": getattr(gs, "upi_id", "") if gs else "",
                "payee_name": getattr(gs, "upi_payee_name", "") if gs else "",
            }
        elif provider.name == "razorpay":
            gs = self._settings()
            data["razorpay"] = {
                "key_id": getattr(gs, "razorpay_key_id", "") if gs else "",
            }

        return Response(data)

    @staticmethod
    def _settings():
        try:
            from global_settings.models import GlobalSettings
            return GlobalSettings.load()
        except Exception:
            return None
