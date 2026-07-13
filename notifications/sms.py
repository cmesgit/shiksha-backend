# PLACEMENT: backend/backend/notifications/sms.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/sms.py
#
# The SMS channel. Three hard rules baked in:
#
#   1. TEMPLATES ONLY, NEVER FREEFORM. Every SMS body to an Indian number
#      must byte-match a template registered on a TRAI DLT portal (Jio
#      TrueConnect / Airtel / Vi / Vodafone DLT). Carriers "scrub" each
#      message against the registered template — a mismatch is silently
#      dropped and still billed. So callers pass a template KEY +
#      variables; the text lives in settings.SMS_TEMPLATES where the
#      Python format string mirrors the registered {#var#} wording 1:1.
#
#   2. NEVER RAISES to the caller. SMS is best-effort; business logic
#      must not 500 because a carrier hiccuped. Failures land in SmsLog.
#
#   3. EVERY ATTEMPT IS LOGGED (SmsLog): sent / failed / skipped + why.
#      That's your DLT audit trail and your "why didn't the parent get
#      the SMS" support answer.
#
# Provider selection: settings.SMS_PROVIDER = console | msg91 | twilio
#   console — dev default, prints to log, always "sent". No keys needed.
#   msg91   — RECOMMENDED for production (Indian DLT-native, ~₹0.15–0.25
#             per transactional SMS). Uses the Flow API: the DLT template
#             is configured as a Flow on the MSG91 dashboard, we send the
#             flow/template id + named variables. Sender ID (6-char DLT
#             header, e.g. SHIKSA) is attached to the Flow on their side.
#   twilio  — international students / fallback. NOTE: Twilio also
#             routes Indian traffic through DLT — you still register the
#             entity + templates and map them in the Twilio console, and
#             per-SMS cost to India is roughly 3–4× MSG91.

import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MSG91_FLOW_ENDPOINT = "https://control.msg91.com/api/v5/flow"


def render_template(template_key, variables):
    """(fallback_text, msg91_flow_id) for a template key, or (None, None)
    if the key isn't configured. fallback_text is what console/twilio
    send; msg91 sends the flow id + raw variables instead."""
    spec = (getattr(settings, "SMS_TEMPLATES", {}) or {}).get(template_key)
    if not spec:
        return None, None
    text = spec.get("text", "")
    try:
        rendered = text.format(**(variables or {}))
    except (KeyError, IndexError) as exc:
        logger.error("sms: template %r missing variable %s", template_key, exc)
        return None, None
    return rendered, spec.get("msg91_flow_id") or ""


# ── Providers ───────────────────────────────────────────────────────────

def _send_console(to, text, template_key, variables):
    logger.info("SMS[console] → %s :: %s", to, text)
    print(f"SMS[console] → {to} :: {text}")  # visible in runserver output
    return True, "console", ""


def _send_msg91(to, text, template_key, variables):
    auth_key = getattr(settings, "MSG91_AUTH_KEY", "")
    _, flow_id = render_template(template_key, variables)
    if not auth_key:
        return False, "", "MSG91_AUTH_KEY not configured"
    if not flow_id:
        return False, "", f"no msg91_flow_id for template {template_key!r}"

    recipient = {"mobiles": to.lstrip("+")}          # MSG91 wants 91xxxxxxxxxx
    recipient.update({k: str(v) for k, v in (variables or {}).items()})
    payload = {"template_id": flow_id, "short_url": "0",
               "recipients": [recipient]}
    try:
        resp = requests.post(
            MSG91_FLOW_ENDPOINT,
            headers={"authkey": auth_key, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=10,
        )
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        if resp.ok and body.get("type") == "success":
            return True, str(body.get("message", "")), ""
        return False, "", f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, "", f"request error: {exc}"


def _send_twilio(to, text, template_key, variables):
    sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    sender = getattr(settings, "TWILIO_FROM", "")
    if not (sid and token and sender):
        return False, "", "TWILIO_* settings not configured"
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"To": to, "From": sender, "Body": text},
            timeout=10,
        )
        if resp.ok:
            return True, resp.json().get("sid", ""), ""
        return False, "", f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, "", f"request error: {exc}"


_PROVIDERS = {"console": _send_console, "msg91": _send_msg91,
              "twilio": _send_twilio}


# ── Entry point ─────────────────────────────────────────────────────────

def send_sms(to, template_key, variables=None, verb="", user=None,
             phone_source=""):
    """Send one templated SMS. Returns True if the provider accepted it.
    Always writes an SmsLog row; never raises."""
    from .models import SmsLog  # lazy: importable pre-migrate

    provider_name = getattr(settings, "SMS_PROVIDER", "console")
    log = SmsLog(user=user, to=to or "", verb=verb,
                 template_key=template_key or "", provider=provider_name,
                 phone_source=phone_source)

    try:
        if not to:
            log.status, log.error = SmsLog.STATUS_SKIPPED, "no phone number"
            return False

        text, _ = render_template(template_key, variables)
        if text is None:
            log.status = SmsLog.STATUS_SKIPPED
            log.error = f"template {template_key!r} not in SMS_TEMPLATES"
            return False
        log.body = text

        sender = _PROVIDERS.get(provider_name)
        if sender is None:
            log.status, log.error = SmsLog.STATUS_FAILED, \
                f"unknown SMS_PROVIDER {provider_name!r}"
            return False

        ok, provider_id, error = sender(to, text, template_key, variables)
        log.provider_message_id = provider_id[:100]
        if ok:
            log.status = SmsLog.STATUS_SENT
            return True
        log.status, log.error = SmsLog.STATUS_FAILED, error[:500]
        # Surface as an exception marker for Celery retry (caller decides).
        return False
    except Exception as exc:  # belt & braces — never propagate
        logger.exception("sms: unexpected failure sending %r to %s",
                         template_key, to)
        log.status, log.error = SmsLog.STATUS_FAILED, str(exc)[:500]
        return False
    finally:
        try:
            log.save()
        except Exception:
            logger.exception("sms: could not persist SmsLog")
