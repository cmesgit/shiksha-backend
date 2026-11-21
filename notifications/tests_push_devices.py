# Push device registration — the security property and the degrade path.
#
# Two separate risks are pinned here.
#
# 1. A device token is a DELIVERY ADDRESS for someone's notifications. If a
#    caller could attach a token to another user's id, every notification for
#    that person would arrive on the attacker's phone — a silent, persistent
#    read of someone else's bookings, chats and grades. `config/urls.py` uses
#    `FCMDeviceAuthorizedViewSet` (not `FCMDeviceViewSet`) precisely because
#    the authorized variant forces IsAuthenticated and binds the row to
#    request.user. The viewset is library code, so these assert the BEHAVIOUR
#    we depend on rather than trusting the class name.
#
# 2. `notifications.services.notify()` calls push for ~45 verbs. Neither a
#    missing Firebase credential nor an FCM outage may ever propagate — a
#    booking must not fail to be created because a phone could not be reached.

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from fcm_django.models import FCMDevice
from notifications.push import push_to_users

TOKEN_A = "fake-registration-id-aaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "fake-registration-id-bbbbbbbbbbbbbbbbbbbbbbbb"


class PushDeviceRegistrationTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username="alice@example.com", email="alice@example.com",
            password="pw-alice-12345",
        )
        self.mallory = User.objects.create_user(
            username="mallory@example.com", email="mallory@example.com",
            password="pw-mallory-12345",
        )
        self.url = reverse("fcmdevice-list")

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_authenticated_user_can_register_a_device(self):
        res = self._client(self.alice).post(
            self.url, {"registration_id": TOKEN_A, "type": "android"},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.content[:200])
        device = FCMDevice.objects.get(registration_id=TOKEN_A)
        self.assertEqual(device.user_id, self.alice.id)
        self.assertTrue(device.active)

    def test_anonymous_cannot_register(self):
        res = APIClient().post(
            self.url, {"registration_id": TOKEN_A, "type": "android"},
            format="json",
        )
        self.assertIn(res.status_code, (401, 403), res.content[:200])
        self.assertFalse(FCMDevice.objects.exists())

    def test_a_token_cannot_be_attached_to_someone_elses_account(self):
        """🔴 Posting another user's id must not bind the device to them.

        This is the whole reason for the *Authorized* viewset. If this ever
        fails, the attacker receives the victim's push notifications — and
        nothing on either account looks wrong.
        """
        res = self._client(self.mallory).post(
            self.url,
            {"registration_id": TOKEN_B, "type": "android",
             "user": self.alice.id},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201, 400), res.content[:200])
        if FCMDevice.objects.filter(registration_id=TOKEN_B).exists():
            device = FCMDevice.objects.get(registration_id=TOKEN_B)
            self.assertEqual(
                device.user_id, self.mallory.id,
                "a device was registered against a DIFFERENT user than the "
                "caller — that user's notifications now go to the caller",
            )

    def test_one_user_may_hold_several_devices(self):
        """ONE_DEVICE_PER_USER is False on purpose.

        A learner with a phone and a tablet, or a shared phone carrying
        several profiles, must not have one registration silently deactivate
        the other.
        """
        c = self._client(self.alice)
        c.post(self.url, {"registration_id": TOKEN_A, "type": "android"},
               format="json")
        c.post(self.url, {"registration_id": TOKEN_B, "type": "android"},
               format="json")
        self.assertEqual(
            FCMDevice.objects.filter(user=self.alice, active=True).count(), 2,
        )

    def test_a_user_cannot_see_another_users_devices(self):
        FCMDevice.objects.create(
            user=self.alice, registration_id=TOKEN_A, type="android",
        )
        res = self._client(self.mallory).get(self.url)
        self.assertEqual(res.status_code, 200, res.content[:200])
        body = res.json()
        rows = body if isinstance(body, list) else body.get("results", [])
        self.assertNotIn(
            TOKEN_A, [r.get("registration_id") for r in rows],
            "one user can enumerate another user's device tokens",
        )


class PushSendDegradesTest(TestCase):
    """push_to_users must never raise — notify() calls it for ~45 verbs."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="bob@example.com", email="bob@example.com",
            password="pw-bob-12345",
        )

    def test_no_devices_is_zero_not_an_error(self):
        self.assertEqual(push_to_users(self.user.id, "Title", "Body"), 0)

    def test_an_fcm_outage_does_not_propagate(self):
        """🔴 A booking must not fail because Firebase is down.

        `FIREBASE_APP` is overridden to a sentinel on purpose. `push_to_users`
        short-circuits when it is None, so without this the call would return
        0 before ever reaching `send_message` and this test would pass while
        exercising nothing — it would assert the no-credential path twice and
        the outage path never.
        """
        FCMDevice.objects.create(
            user=self.user, registration_id=TOKEN_A, type="android",
        )
        with self.settings(FIREBASE_APP=object()), patch(
            "fcm_django.models.FCMDeviceQuerySet.send_message",
            side_effect=RuntimeError("FCM exploded"),
        ) as send:
            self.assertEqual(push_to_users(self.user.id, "Title", "Body"), 0)
        self.assertTrue(send.called, "the send path was never reached")

    def test_accepts_a_single_id_or_a_list(self):
        self.assertEqual(push_to_users(self.user.id, "T"), 0)
        self.assertEqual(push_to_users([self.user.id], "T"), 0)

    def test_no_firebase_credential_is_a_no_op_not_a_crash(self):
        """🔴 Both servers have no credential today, and tokens still register.

        The whole deploy ordering depends on this: ship the client, let tokens
        accumulate, add the key later. If an absent credential raised, every
        verb that notifies would break the moment the app shipped.
        """
        from django.conf import settings

        self.assertIsNone(
            settings.FIREBASE_APP,
            "this test asserts the uncredentialled path; set-up has changed",
        )
        FCMDevice.objects.create(
            user=self.user, registration_id=TOKEN_B, type="android",
        )
        self.assertEqual(push_to_users(self.user.id, "Title", "Body"), 0)
