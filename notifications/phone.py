# PLACEMENT: backend/backend/notifications/phone.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/phone.py
#
# Two jobs:
#   1. normalize_msisdn() — turn whatever a user typed into E.164 (+91…).
#      The profile forms never validated phone format, so the DB holds
#      "9876543210", "09876...", "91-98765 43210", "+91 98765-43210" …
#   2. phone_for_user() — decide WHICH number an SMS for this user goes
#      to. This is the awkward part of the data model today:
#
#        · LearnerProfile.phone            (SELF profiles usually filled)
#        · LearnerProfile.father/mother/guardian_phone (dependents)
#        · TeacherProfile.phone            (NEW — added in this change;
#                                           empty until signup/profile UI
#                                           collects it)
#        · CounselorProfile.phone          (NEW — same caveat)
#        · accounts.User has NO phone field at all.
#
#      Resolution returns (msisdn, source) so SmsLog can record where the
#      number came from, and (None, reason) so a skipped SMS is auditable.

import re

from django.conf import settings

_DIGITS = re.compile(r"\D+")


def normalize_msisdn(raw, default_cc=None):
    """Best-effort E.164 for Indian-first data. Returns '+91xxxxxxxxxx'
    or None if it can't be a valid mobile number.

    Rules (in order):
      · strip everything non-digit (keep a leading +'s intent)
      · 10 digits starting 6-9        → +91 + digits
      · 11 digits starting 0          → drop the trunk 0, treat as above
      · 12 digits starting 91         → '+' + digits
      · already-plus'd 11-15 digits   → '+' + digits (international, kept)
      · anything else                 → None
    """
    if not raw:
        return None
    raw = str(raw).strip()
    had_plus = raw.startswith("+")
    digits = _DIGITS.sub("", raw)
    if not digits:
        return None

    cc = (default_cc or getattr(settings, "SMS_DEFAULT_COUNTRY_CODE", "+91")).lstrip("+")

    if len(digits) == 10 and digits[0] in "6789":
        return f"+{cc}{digits}"
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return f"+{cc}{digits[1:]}"
    if len(digits) == 12 and digits.startswith(cc):
        return f"+{digits}"
    if had_plus and 11 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def _learner_phone(lp):
    """A learner profile's reachable number: the learner's own phone,
    else parent/guardian in a stable order. Dependent children often have
    no phone of their own — the guardian IS the correct recipient."""
    if lp is None:
        return None, None
    for value, source in (
        (lp.phone, "learner"),
        (lp.father_phone, "father"),
        (lp.mother_phone, "mother"),
        (lp.guardian_phone, "guardian"),
    ):
        msisdn = normalize_msisdn(value)
        if msisdn:
            return msisdn, f"learner_profile:{source}"
    return None, None


def phone_for_user(user, learner_profile=None):
    """Resolve the SMS destination for `user`.

    learner_profile — pass the appointment/session's LearnerProfile when
    the notification is about a specific (possibly dependent) learner so
    the SMS reaches that child's guardian, not a sibling's.

    Returns (msisdn, source) or (None, "no_phone").
    """
    # 1. The specific learner this event is about.
    msisdn, source = _learner_phone(learner_profile)
    if msisdn:
        return msisdn, source

    if user is None:
        return None, "no_phone"

    # 2. Teacher / counselor profile numbers (fields added in this change).
    for attr, source in (("teacher_profile", "teacher_profile"),
                         ("counselor_profile", "counselor_profile")):
        profile = getattr(user, attr, None)
        if profile is not None:
            msisdn = normalize_msisdn(getattr(profile, "phone", ""))
            if msisdn:
                return msisdn, source

    # 3. Any learner profile on the account, SELF first (the account
    #    holder's own number), then defaults, then the rest.
    try:
        profiles = list(user.learner_profiles.all())
    except Exception:
        profiles = []
    profiles.sort(key=lambda p: (p.relationship != "SELF", not p.is_default))
    for lp in profiles:
        msisdn, source = _learner_phone(lp)
        if msisdn:
            return msisdn, source

    return None, "no_phone"
