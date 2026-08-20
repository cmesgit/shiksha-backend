# One channel failing must never suppress the other two.
#
# notify()'s three sends used to share a single try/except, with email
# dispatched first. A Resend outage therefore aborted the block before the
# SMS or push was attempted — the notification looked delivered (the durable
# row and the bell were already written above), and the only trace was one
# generic "channel dispatch failed" line.
#
# It stayed invisible because the time-critical reminders had email=OFF and
# never entered that path at all. Turning email on for them (the only wired
# channel — see policy.py's header) would have made a transient email error
# silently take the SMS down with it, on exactly the messages that most need
# to arrive. These tests pin the isolation so the shared-try shape cannot
# come back.

from unittest.mock import patch

from django.test import TestCase

from accounts.models import User
from notifications import policy as P
from notifications.models import Notification

DISPATCH = "notifications.services._dispatch_%s"


class ChannelIsolationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="learner@example.com", email="learner@example.com",
            password="x", first_name="Lear")
        # A verb routed to all three channels, so every ordering is exercised.
        self.verb = "session.reminder_1h"
        rules = P.for_verb(self.verb)
        assert rules["email"] != P.OFF, (
            "this test assumes the 1h reminder reaches email; if that routing "
            "changed, the isolation still matters — pick another verb")

    def _notify(self):
        from notifications.services import notify
        return notify(recipient=self.user, verb=self.verb,
                      title="Your session starts in an hour", body="Join now")

    def test_email_failure_does_not_suppress_sms_or_push(self):
        with patch(DISPATCH % "email", side_effect=RuntimeError("resend down")), \
             patch(DISPATCH % "sms") as sms, \
             patch(DISPATCH % "push") as push:
            self._notify()
        self.assertTrue(sms.called, "email failure swallowed the SMS")
        self.assertTrue(push.called, "email failure swallowed the push")

    def test_sms_failure_does_not_suppress_push(self):
        with patch(DISPATCH % "email"), \
             patch(DISPATCH % "sms", side_effect=RuntimeError("msg91 down")), \
             patch(DISPATCH % "push") as push:
            self._notify()
        self.assertTrue(push.called, "SMS failure swallowed the push")

    def test_durable_row_survives_every_channel_failing(self):
        """The bell is the one delivery that must never depend on a provider."""
        boom = RuntimeError("all providers down")
        with patch(DISPATCH % "email", side_effect=boom), \
             patch(DISPATCH % "sms", side_effect=boom), \
             patch(DISPATCH % "push", side_effect=boom):
            self._notify()
        self.assertEqual(
            Notification.objects.filter(recipient=self.user,
                                        verb=self.verb).count(), 1)


class TimeCriticalReachabilityTest(TestCase):
    """The check in checks.py, asserted as a unit so a policy edit that
    strands a reminder fails CI rather than waiting for `manage.py check` to
    be read by a human at deploy time."""

    def test_every_reminder_keeps_a_route_through_email(self):
        # Email is the only channel configured in production today. Any
        # reminder routed solely to SMS/push reaches nobody.
        sending = {P.OPT_OUT, P.REQUIRED}
        stranded = [
            verb for verb, spec in P.POLICY.items()
            if spec.get("category") == "reminders"
            and any(spec.get(c) in sending for c in ("email", "sms", "push"))
            and spec.get("email") not in sending
        ]
        self.assertEqual(stranded, [], (
            "these reminders cannot reach a user who is outside the app: "
            f"{stranded}. Either wire SMS/push, or route them through email."))
