# PLACEMENT: backend/backend/notifications/checks.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/checks.py
#
# Deploy-time guard against the failure mode described at the top of
# policy.py: a notification whose only routes are channels this deployment
# has never configured. Nothing raises, nothing 500s, no log line appears —
# the message is simply never delivered, and SMS actively reports success
# while doing it. The only way to notice is a student saying "I never got
# told my class started", which is exactly how it was found.
#
# So the invariant is checked mechanically instead: every TIME-CRITICAL verb
# (one whose whole purpose is reaching someone who is NOT looking at the
# app) must keep at least one channel that is actually wired here.
#
# Warning, never Error: an unconfigured channel is a deployment fact, not a
# code defect, and `manage.py` must stay usable on a laptop with no Resend
# key. For that reason the check is a no-op under DEBUG — locally nothing is
# configured and a permanent wall of warnings just trains people to ignore
# it. It speaks up on the boxes where silence actually costs a class.

from django.conf import settings
from django.core.checks import Warning as CheckWarning, register

# Verbs that must survive the user being outside the app. Derived from the
# policy's own category rather than hardcoded, so a reminder added later is
# covered automatically instead of quietly inheriting the original bug.
_TIME_CRITICAL_CATEGORIES = {"reminders"}
_TIME_CRITICAL_VERBS = {"livestream.started"}


def configured_channels():
    """Which away-from-app channels can actually deliver on THIS box.

    Deliberately mirrors what each sender checks at runtime, so the check
    cannot drift into disagreeing with reality:
      - email  resend.py needs an API key
      - sms    sms.py falls back to the `console` provider, which only logs
      - push   push.py is a documented no-op until fcm-django is installed
    """
    email = bool(getattr(settings, "RESEND_API_KEY", "")
                 or getattr(settings, "EMAIL_HOST_USER", ""))
    sms = getattr(settings, "SMS_PROVIDER", "console") not in ("", "console")
    try:
        import fcm_django  # noqa: F401
        push = True
    except ImportError:
        push = False
    return {"email": email, "sms": sms, "push": push}


@register()
def check_time_critical_verbs_deliverable(app_configs, **kwargs):
    if getattr(settings, "DEBUG", False):
        return []

    from . import policy as P

    live = configured_channels()
    # A box with NO channel wired at all is a laptop or a test runner, not a
    # deployment — every verb would trip and the wall of warnings would just
    # train people to scroll past it. (DEBUG alone is not enough of a signal:
    # config/settings_test.py runs with DEBUG=False.) On a real box email is
    # configured, so the check keeps its teeth exactly where it matters.
    if not any(live.values()):
        return []
    sending_levels = {P.OPT_OUT, P.REQUIRED, getattr(P, "OPT_IN", "opt_in")}
    errors = []

    for verb, spec in sorted(P.POLICY.items()):
        critical = (spec.get("category") in _TIME_CRITICAL_CATEGORIES
                    or verb in _TIME_CRITICAL_VERBS)
        if not critical:
            continue
        intended = [c for c in ("email", "sms", "push")
                    if spec.get(c) in sending_levels]
        if intended and not any(live[c] for c in intended):
            errors.append(CheckWarning(
                f"Notification '{verb}' reaches nobody outside the app.",
                hint=(f"policy.py routes it to {', '.join(intended)}, none of "
                      f"which is configured here (configured: "
                      f"{', '.join(k for k, v in live.items() if v) or 'none'}). "
                      f"The in-app bell still shows it, but no reminder leaves "
                      f"the server. Either wire the channel or give this verb a "
                      f"route through one that is already working."),
                id="notifications.W001",
            ))
    return errors
