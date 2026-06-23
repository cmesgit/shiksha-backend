"""
enrollments/payments.py — pluggable payment layer.

The platform must run "free for now" but stay one setting away from a real
gateway. Everything that decides *how a course is paid for* goes through a
provider object, selected by ``settings.PAYMENT_PROVIDER``:

    PAYMENT_PROVIDER = "free"        # default — no payment, access is instant
    PAYMENT_PROVIDER = "manual_upi"  # student submits UTR + receipt, admin approves
    PAYMENT_PROVIDER = "razorpay"    # gateway (stub — wire when you go live)

To attach a new gateway later: subclass ``PaymentProvider``, implement
``create_intent`` / ``verify``, register it in ``_PROVIDERS``, and flip the
setting. No call sites change.

Add to settings (settings_base.py):

    import os
    PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "free")
"""
from django.conf import settings


class PaymentProvider:
    name = "base"
    label = "Base"

    # Does a paid course need manual proof (UTR + receipt) before approval?
    requires_manual_proof = False
    # Should a request be granted access immediately on submission (no admin)?
    auto_activate = False
    # Is money actually collected? (False while free.)
    collects_money = False

    def initial_status(self, course=None):
        """Status a new EnrollmentRequest gets on submission."""
        from .models import EnrollmentRequest
        return (
            EnrollmentRequest.STATUS_APPROVED
            if self.auto_activate
            else EnrollmentRequest.STATUS_PENDING
        )

    def create_intent(self, *, user, course, request_obj=None):
        """Return gateway payload (order id, key, amount…) or None.

        Override for real gateways. Returning None means "nothing to pay now".
        """
        return None

    def verify(self, *, request_obj, payload):
        """Verify a gateway callback/signature. Override for real gateways."""
        return True

    def describe(self):
        return {
            "provider": self.name,
            "label": self.label,
            "requires_manual_proof": self.requires_manual_proof,
            "auto_activate": self.auto_activate,
            "collects_money": self.collects_money,
            "is_free": self.name == "free",
        }


class FreeProvider(PaymentProvider):
    name = "free"
    label = "Free (no payment)"
    requires_manual_proof = False
    auto_activate = True
    collects_money = False


class ManualUpiProvider(PaymentProvider):
    name = "manual_upi"
    label = "Manual UPI + admin approval"
    requires_manual_proof = True
    auto_activate = False
    collects_money = True


class RazorpayProvider(PaymentProvider):
    """Stub. Fill in when you're ready to collect money via Razorpay."""
    name = "razorpay"
    label = "Razorpay"
    requires_manual_proof = False
    auto_activate = False          # access is granted on verified payment
    collects_money = True

    def create_intent(self, *, user, course, request_obj=None):
        raise NotImplementedError(
            "Razorpay is not wired yet. Create the order here and return "
            "{'order_id': ..., 'key_id': ..., 'amount': ...}."
        )

    def verify(self, *, request_obj, payload):
        raise NotImplementedError(
            "Verify the Razorpay signature here before granting access."
        )


_PROVIDERS = {
    p.name: p
    for p in (FreeProvider, ManualUpiProvider, RazorpayProvider)
}


def get_active_payment_mode():
    """The payment mode in force right now.

    Reads the GlobalSettings singleton (admin-toggleable, no restart). The
    free-trial master switch wins: while it's on, everything is free. Falls
    back to ``settings.PAYMENT_PROVIDER`` (env var) if the settings table
    isn't available yet — e.g. before the global_settings app is migrated.
    """
    try:
        from global_settings.models import GlobalSettings
        return GlobalSettings.load().effective_mode
    except Exception:
        return getattr(settings, "PAYMENT_PROVIDER", "free")


def get_payment_provider():
    """The active provider object for the current payment mode."""
    cls = _PROVIDERS.get(get_active_payment_mode(), FreeProvider)
    return cls()
