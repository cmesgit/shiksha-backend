from rest_framework.throttling import UserRateThrottle, AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    scope = "login"


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
