from rest_framework.throttling import UserRateThrottle, AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Per-IP cap on login attempts.

    ⚠️ This class existed, was imported, and had a configured rate
    ("login": "20/min") for a long time WITHOUT BEING ATTACHED TO ANY VIEW —
    so the real LoginView (accounts/auth_flow.py) accepted unlimited password
    guesses. Attached now; see LoginAccountRateThrottle below for why one
    throttle is not enough.
    """
    scope = "login"


class LoginAccountRateThrottle(AnonRateThrottle):
    """Per-EMAIL cap on login attempts, complementing the per-IP one.

    IP-only throttling has a bad failure mode in this product: a school or a
    family behind one NAT address shares a single IP, so a strict per-IP cap
    punishes a whole class logging in at 9am while barely inconveniencing an
    attacker with a handful of addresses. Keying on the submitted email
    instead makes credential-stuffing against ONE account expensive no matter
    how many IPs it comes from, and leaves the legitimate crowd alone.

    Falls back to the IP when no email was supplied, so a malformed flood is
    still bounded.
    """
    scope = "login_account"

    def get_cache_key(self, request, view):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return super().get_cache_key(request, view)
        return self.cache_format % {"scope": self.scope, "ident": email}


class SignupRateThrottle(AnonRateThrottle):
    scope = "signup"


class ResendVerificationRateThrottle(UserRateThrottle):
    scope = "resend_verification"


class PasswordResetRequestRateThrottle(AnonRateThrottle):
    scope = "password_reset_request"


class PasswordResetVerifyRateThrottle(AnonRateThrottle):
    scope = "password_reset_verify"


class PinVerifyRateThrottle(UserRateThrottle):
    # Profile-switch PIN check (ProfileSelectView) had no throttle at all —
    # a 4-6 digit PIN with unlimited guesses is brute-forceable. Keyed per
    # authenticated user (the endpoint already requires IsAuthenticated),
    # same as ResendVerificationRateThrottle.
    scope = "pin_verify"
