import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_gmail(to, subject, message_text, html=None):
    """Send transactional email via the Resend HTTPS API.

    Name kept as ``send_gmail`` for backward compatibility with existing
    callers. Transport is the Resend REST API over port 443 (works on
    hosts where outbound SMTP is blocked, e.g. DigitalOcean droplets).
    """
    api_key = getattr(settings, "RESEND_API_KEY", "") or ""
    if not api_key:
        raise RuntimeError(
            "RESEND_API_KEY is not configured; cannot send email."
        )

    payload = {
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "text": message_text,
    }
    if html:
        payload["html"] = html

    response = requests.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )

    if not response.ok:
        logger.error(
            "Resend API error (%s) sending to %s: %s",
            response.status_code, to, response.text,
        )
        response.raise_for_status()
