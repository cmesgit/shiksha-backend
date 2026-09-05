"""content/contact_views.py — the public /contact form's submit endpoint.

Kept out of ``content/views.py`` on purpose: that module's contract, stated in
its own header, is "public, read-only endpoints; writes happen only through
Django admin". This is a write, and an unauthenticated one, so it lives beside
``ai_views.py`` instead — the other public endpoint that does not fit that rule.

Threat model, since this is an anonymous write reachable by anyone:

* **Throttled** per IP (``contact_form`` scope) exactly like the notify-me
  endpoints, which were the previous anonymous-write precedent.
* **Honeypot** field: bots fill every input they find. A non-empty ``website``
  is accepted with a normal 201 and silently dropped, so a scripted submitter
  gets no signal that it was caught.
* **Bounded lengths** on every field, enforced by the serializer rather than
  only by the column, so an oversized body is rejected before it is stored.
* **No email is ever sent to the address the visitor typed.** The notification
  goes to our own inbox only. Echoing a confirmation back to an arbitrary
  attacker-supplied address would turn this into an open relay for sending
  ShikshaCom-branded mail to strangers.
"""
import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import ContactMessage, NewsletterSubscriber

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000


class ContactMessageSerializer(serializers.ModelSerializer):
    """Write-only serializer for an anonymous enquiry.

    ``consent`` is a required input that must be True — the form states the
    visitor agrees to be contacted about the enquiry, and accepting a
    submission without it would store personal details on a basis they never
    agreed to. It is translated to the model's ``consented_at`` timestamp in
    the view rather than stored as a uniformly-True boolean.
    """

    consent = serializers.BooleanField(write_only=True)
    # The honeypot. Named like a field a bot wants to fill and left entirely
    # out of the model; `required=False` so real submissions never carry it.
    website = serializers.CharField(
        required=False, allow_blank=True, write_only=True, max_length=200,
    )
    message = serializers.CharField(max_length=MAX_MESSAGE_CHARS)

    class Meta:
        model = ContactMessage
        fields = ["name", "email", "phone", "role", "topic", "message",
                  "consent", "website"]

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError(
                "We need your agreement before we can reply to your enquiry."
            )
        return value

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Please tell us your name.")
        return value

    def validate_message(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Please write a short message.")
        return value


def _client_ip(request):
    """Best-effort caller IP.

    ``USE_X_FORWARDED_HOST`` is on and the app sits behind nginx, so
    REMOTE_ADDR is the proxy. Take the FIRST hop of X-Forwarded-For, which is
    the client as recorded by our own edge. Anything further left in that
    header is attacker-controlled and is not trusted for anything beyond a
    forensic breadcrumb — which is all this field is ever used for.
    """
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _notify_team(msg):
    """Email the enquiry to our own inbox. Best effort, never fatal.

    The row is already committed by the time this runs, so a dead mail
    provider costs a notification, not the enquiry itself. That ordering is
    the whole reason the model exists.
    """
    recipient = getattr(settings, "CONTACT_FORM_RECIPIENT", "") or ""
    if not recipient:
        logger.warning("contact form: CONTACT_FORM_RECIPIENT unset; not emailing #%s", msg.pk)
        return
    try:
        from accounts.email_utils import send_gmail

        send_gmail(
            recipient,
            f"[Contact form] {msg.get_topic_display()} — {msg.name}",
            (
                f"Name:    {msg.name}\n"
                f"Email:   {msg.email}\n"
                f"Phone:   {msg.phone or '—'}\n"
                f"They are: {msg.get_role_display()}\n"
                f"Topic:   {msg.get_topic_display()}\n"
                f"\n{msg.message}\n"
                f"\n— Sent from the ShikshaCom contact form. Reply to {msg.email}.\n"
            ),
        )
    except Exception:
        # Logged with the row id so the enquiry is findable in the admin even
        # when the mail never arrived.
        logger.exception("contact form: notification email failed for #%s", msg.pk)


class ContactMessageCreateView(APIView):
    """POST /api/content/contact/ — send a message from the public contact page.

    Public: /contact has no login gate, so this cannot require
    IsAuthenticated. Returns 201 with a bare ``{"ok": true}`` — deliberately
    no echo of what was stored and no row id, since an anonymous caller has no
    way to be authorised to read it back.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact_form"

    def post(self, request):
        serializer = ContactMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Honeypot tripped: look identical to success from the outside.
        if data.pop("website", "").strip():
            logger.info("contact form: honeypot tripped from %s", _client_ip(request))
            return Response({"ok": True}, status=status.HTTP_201_CREATED)

        data.pop("consent")
        msg = ContactMessage.objects.create(
            **data,
            consented_at=timezone.now(),
            submitted_ip=_client_ip(request),
        )
        _notify_team(msg)
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class NewsletterSubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    website = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
    )


class NewsletterSubscribeView(APIView):
    """POST /api/content/newsletter/ — the contact page's closing CTA band.

    Shares the ``contact_form`` throttle scope with the enquiry endpoint on
    purpose: they sit on the same page and a bot hitting one will hit the
    other, so one shared budget per IP is the honest limit.

    Re-subscribing is idempotent and, importantly, does NOT clear
    ``unsubscribed_at`` — anyone can type someone else's address into a public
    box, and letting that re-add a person who opted out would make the
    unsubscribe meaningless. The response is identical either way so the box
    cannot be used to probe who is on the list.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact_form"

    def post(self, request):
        serializer = NewsletterSubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("website", "").strip():
            logger.info("newsletter: honeypot tripped from %s", _client_ip(request))
            return Response({"ok": True}, status=status.HTTP_201_CREATED)

        NewsletterSubscriber.objects.get_or_create(
            email=serializer.validated_data["email"].lower(),
            defaults={"submitted_ip": _client_ip(request)},
        )
        return Response({"ok": True}, status=status.HTTP_201_CREATED)
