# PLACEMENT: backend/content/tests/test_contact_form.py
#
# The public /contact form's submit endpoint. Run with:
#     python manage.py test content.tests.test_contact_form
#
# The endpoint is anonymous and writes to the database, so most of what is
# worth asserting here is about what it REFUSES, not what it accepts.

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from content.models import ContactMessage, NewsletterSubscriber

VALID = {
    "name": "Ananya Sharma",
    "email": "ananya@example.com",
    "phone": "+91 90000 00000",
    "role": ContactMessage.Role.STUDENT,
    "topic": ContactMessage.Topic.ADMISSIONS,
    "message": "Is the Class 10 CBSE batch still open for this session?",
    "consent": True,
}


class ContactFormTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("content:contact-create")
        # Throttle state lives in the cache and is not reset between test
        # methods. settings_test disables every scope, but clear anyway so
        # this class does not depend on that staying true.
        cache.clear()

        # Every test but the throttling one wants the notification email to be
        # a no-op. Without this the real send_gmail runs, raises "RESEND_API_KEY
        # is not configured", and _notify_team logs a full traceback — four of
        # them per run, which is exactly the noise that hides a real error.
        patcher = patch("accounts.email_utils.send_gmail")
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **overrides):
        payload = {**VALID, **overrides}
        return self.client.post(self.url, payload, format="json")

    # ── the happy path ────────────────────────────────────────────

    def test_anonymous_visitor_can_send_a_message(self):
        res = self.post()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data, {"ok": True})

        msg = ContactMessage.objects.get()
        self.assertEqual(msg.name, "Ananya Sharma")
        self.assertEqual(msg.email, "ananya@example.com")
        self.assertEqual(msg.message, VALID["message"])
        self.assertEqual(msg.status, ContactMessage.Status.NEW)

    def test_consent_is_stored_as_a_timestamp(self):
        """A uniformly-True boolean answers nothing; the time does."""
        self.post()
        self.assertIsNotNone(ContactMessage.objects.get().consented_at)

    def test_response_does_not_echo_the_stored_row(self):
        """An anonymous caller has no standing to read an enquiry back, so the
        response must not hand them an id to try."""
        res = self.post()
        self.assertNotIn("id", res.data)

    def test_phone_is_optional(self):
        res = self.post(phone="")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(ContactMessage.objects.get().phone, "")

    # ── what it refuses ───────────────────────────────────────────

    def test_consent_is_required(self):
        res = self.post(consent=False)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_missing_consent_key_is_rejected(self):
        payload = {k: v for k, v in VALID.items() if k != "consent"}
        res = self.client.post(self.url, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_invalid_email_is_rejected(self):
        res = self.post(email="not-an-address")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_blank_name_and_message_are_rejected(self):
        for field in ("name", "message"):
            with self.subTest(field=field):
                ContactMessage.objects.all().delete()
                res = self.post(**{field: "   "})
                self.assertEqual(res.status_code, 400)
                self.assertFalse(ContactMessage.objects.exists())

    def test_oversized_message_is_rejected_before_storage(self):
        res = self.post(message="x" * 4001)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_unknown_topic_is_rejected(self):
        res = self.post(topic="free_money")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    # ── the honeypot ──────────────────────────────────────────────

    def test_honeypot_looks_like_success_but_stores_nothing(self):
        """A bot that fills every field must get no signal it was caught."""
        res = self.post(website="http://spam.example")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data, {"ok": True})
        self.assertFalse(ContactMessage.objects.exists())

    def test_blank_honeypot_is_a_normal_submission(self):
        res = self.post(website="")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(ContactMessage.objects.exists())

    # ── the notification email ────────────────────────────────────

    def test_team_is_emailed_at_the_configured_address(self):
        with self.settings(CONTACT_FORM_RECIPIENT="desk@shikshacom.com"):
            self.post()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], "desk@shikshacom.com")

    def test_no_email_is_ever_sent_to_the_visitor(self):
        """Echoing a confirmation to an attacker-supplied address would make
        this an open relay for ShikshaCom-branded mail."""
        with self.settings(CONTACT_FORM_RECIPIENT="desk@shikshacom.com"):
            self.post(email="victim@example.com")
        for call in self.send.call_args_list:
            self.assertNotEqual(call.args[0], "victim@example.com")

    def test_a_dead_mail_provider_does_not_lose_the_enquiry(self):
        """The row is committed before the email is attempted, so an outage
        costs a notification and not the message itself."""
        self.send.side_effect = RuntimeError("provider down")
        with self.settings(CONTACT_FORM_RECIPIENT="desk@shikshacom.com"):
            res = self.post()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_unset_recipient_still_stores_the_enquiry(self):
        with self.settings(CONTACT_FORM_RECIPIENT=""):
            res = self.post()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(ContactMessage.objects.count(), 1)
        self.send.assert_not_called()


class NewsletterSubscribeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.url = reverse("content:newsletter-subscribe")

    def test_an_address_is_actually_stored(self):
        """The handoff printed 'You are on the list' and stored nothing. This
        is the assertion that makes that sentence true."""
        res = self.client.post(self.url, {"email": "reader@example.com"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(
            NewsletterSubscriber.objects.filter(email="reader@example.com").exists()
        )

    def test_address_is_normalised_to_lowercase(self):
        self.client.post(self.url, {"email": "Reader@Example.COM"}, format="json")
        self.assertEqual(NewsletterSubscriber.objects.get().email, "reader@example.com")

    def test_subscribing_twice_is_idempotent(self):
        for _ in range(2):
            res = self.client.post(self.url, {"email": "a@example.com"}, format="json")
            self.assertEqual(res.status_code, 201)
        self.assertEqual(NewsletterSubscriber.objects.count(), 1)

    def test_resubscribing_does_not_revive_someone_who_opted_out(self):
        """Anyone can type anyone's address into a public box. If that cleared
        the opt-out, unsubscribing would mean nothing."""
        from django.utils import timezone as tz
        sub = NewsletterSubscriber.objects.create(
            email="gone@example.com", unsubscribed_at=tz.now(),
        )
        res = self.client.post(self.url, {"email": "gone@example.com"}, format="json")
        self.assertEqual(res.status_code, 201)
        sub.refresh_from_db()
        self.assertIsNotNone(sub.unsubscribed_at)

    def test_invalid_email_is_rejected(self):
        res = self.client.post(self.url, {"email": "nope"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(NewsletterSubscriber.objects.exists())

    def test_honeypot_stores_nothing(self):
        res = self.client.post(
            self.url, {"email": "bot@example.com", "website": "http://spam"}, format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertFalse(NewsletterSubscriber.objects.exists())


class ContactFormThrottleTests(TestCase):
    """The form is anonymous, so an unbounded one is a free spam cannon.

    settings_test disables every throttle scope (they make the suite
    order-dependent). @override_settings cannot re-enable one: DRF binds
    THROTTLE_RATES as a CLASS attribute at import time, so the setting change
    never reaches the throttle. Same approach as
    accounts.tests_lookup.LoginThrottleTest.
    """

    def setUp(self):
        cache.clear()
        self.url = reverse("content:contact-create")
        self.client = APIClient()

        patcher = patch("accounts.email_utils.send_gmail")
        patcher.start()
        self.addCleanup(patcher.stop)

        self._saved = ScopedRateThrottle.THROTTLE_RATES.get("contact_form")
        ScopedRateThrottle.THROTTLE_RATES["contact_form"] = "10/hour"
        self.addCleanup(self._restore)

    def _restore(self):
        ScopedRateThrottle.THROTTLE_RATES["contact_form"] = self._saved

    def test_an_eleventh_submission_from_one_ip_is_refused(self):
        codes = [
            self.client.post(self.url, VALID, format="json",
                             REMOTE_ADDR="203.0.113.9").status_code
            for _ in range(11)
        ]
        self.assertEqual(codes[:10], [201] * 10)
        self.assertEqual(codes[10], 429, f"contact form was not throttled: {codes}")
