# M2 regression (Phase 3 §18) — re-verified as part of M3 per this stage's
# "full M0/M1/M2 regression" requirement. No suite for this existed in the
# codebase before this stage; written fresh here.
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.test import TestCase

from accounts.models import LearnerProfile
from notifications.models import Notification
from notifications.services import notify

from .factories import make_learner, make_user


class AudienceIdentityTest(TestCase):

    def test_role_is_derived_from_identity_when_not_given_explicitly(self):
        lp = make_learner()
        notify(
            recipient=lp.account, verb="test.event", title="hi",
            audience_identity=f"L:{lp.id}",
        )
        n = Notification.objects.get(verb="test.event")
        self.assertEqual(n.audience_role, "STUDENT")
        self.assertEqual(n.audience_identity, f"L:{lp.id}")

    def test_blank_audience_identity_is_account_wide_as_before(self):
        account = make_user()
        notify(recipient=account, verb="test.broad", title="hi")
        n = Notification.objects.get(verb="test.broad")
        self.assertEqual(n.audience_identity, "")
        self.assertEqual(n.audience_role, "")


class SiblingIsolationTest(TestCase):
    """The exact bug M2 fixes: two dependent LearnerProfiles on the SAME
    account (siblings) must not see each other's notifications, even
    though they share the one `recipient` User row."""

    def _dashboard_query_for(self, account, learner_profile):
        """Mirrors what a real per-profile dashboard would query: rows
        for this account that are either account-wide (blank) or scoped
        to this exact identity."""
        key = f"L:{learner_profile.id}"
        return Notification.objects.filter(recipient=account).filter(
            Q(audience_identity="") | Q(audience_identity=key)
        )

    def test_sibling_b_does_not_see_a_notification_scoped_to_sibling_a(self):
        account = make_user()
        child_a = make_learner(account=account, display_name="Child A",
                                relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)
        child_b = make_learner(account=account, display_name="Child B",
                                relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)

        notify(
            recipient=account, verb="counseling.booked", title="Session booked for Child A",
            audience_identity=f"L:{child_a.id}",
        )

        a_sees = self._dashboard_query_for(account, child_a)
        b_sees = self._dashboard_query_for(account, child_b)

        self.assertEqual(a_sees.count(), 1)
        self.assertEqual(b_sees.count(), 0)

    def test_account_wide_notification_reaches_both_siblings(self):
        account = make_user()
        child_a = make_learner(account=account, relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)
        child_b = make_learner(account=account, relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)

        notify(recipient=account, verb="payments.receipt", title="Payment received")

        self.assertEqual(self._dashboard_query_for(account, child_a).count(), 1)
        self.assertEqual(self._dashboard_query_for(account, child_b).count(), 1)


class WsEnvelopeTest(TestCase):
    """notify()'s mapping onto the {audience, learner_profile_id} envelope
    UserUpdateConsumer._wanted() filters on — verified against the real
    (in-memory, for tests) channel layer, not by inspecting notify()'s
    internals directly."""

    def test_ws_frame_carries_audience_and_learner_profile_id(self):
        lp = make_learner()
        channel_layer = get_channel_layer()
        test_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"user_updates_{lp.account_id}", test_channel)

        notify(
            recipient=lp.account, verb="test.ws", title="hi",
            audience_identity=f"L:{lp.id}",
        )

        event = async_to_sync(channel_layer.receive)(test_channel)
        self.assertEqual(event["type"], "send_notification")
        self.assertEqual(event["data"]["audience"], "LEARNER")
        self.assertEqual(event["data"]["learner_profile_id"], str(lp.id))

    def test_teacher_identity_maps_to_teacher_audience_with_no_profile_id(self):
        from .factories import make_teacher
        tp = make_teacher()
        channel_layer = get_channel_layer()
        test_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"user_updates_{tp.user_id}", test_channel)

        notify(
            recipient=tp.user, verb="test.ws2", title="hi",
            audience_identity=f"T:{tp.id}",
        )

        event = async_to_sync(channel_layer.receive)(test_channel)
        self.assertEqual(event["data"]["audience"], "TEACHER")
        self.assertNotIn("learner_profile_id", event["data"])
