from django.conf import settings
from django.core.mail import EmailMultiAlternatives


def send_gmail(to, subject, message_text, html=None):
    """Send an email via the configured SMTP backend.

    Name kept as ``send_gmail`` for backward compatibility with existing
    callers; actual transport is now Django's standard SMTP email backend
    (configured via EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD).
    """
    msg = EmailMultiAlternatives(
        subject=subject,
        body=message_text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
    )
    if html:
        msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)
