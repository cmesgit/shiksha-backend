"""The two room kinds share LiveKit plumbing but keep separate access rules."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from .group_session_policy import INSTANT_COLLABORATION, policy_for
from .models import GroupSession, GroupSessionInvite


class GroupSessionPolicyTests(TestCase):
    def setUp(self):
        self.host = User.objects.create_user(
            username="policy_host", email="policy_host@test.com", password="pw",
        )
        self.guest = User.objects.create_user(
            username="policy_guest", email="policy_guest@test.com", password="pw",
        )
        now = timezone.now()
        local_now = timezone.localtime(now)
        self.session = GroupSession.objects.create(
            host=self.host,
            topic="Policy test",
            scheduled_date=local_now.date(),
            scheduled_time=local_now.time().replace(microsecond=0),
            duration_minutes=60,
            session_type="scheduled",
            status="live",
            room_started_at=now,
            room_name="group_session_policy_test",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.guest)
        self.url = f"/api/sessions/group-sessions/{self.session.id}/join/"

    def _join(self):
        with patch("sessions_app.group_session_views.generate_group_session_token",
                   return_value="signed-token"), \
             patch("sessions_app.group_session_views._guest_entitlement",
                   return_value=True):
            return self.client.post(self.url)

    def test_link_holder_can_join_instant_but_not_scheduled(self):
        denied = self._join()
        self.assertEqual(denied.status_code, 403)
        self.assertIn("not a participant", denied.data["error"])

        self.session.session_type = "instant"
        self.session.save(update_fields=["session_type"])
        joined = self._join()
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(joined.data["role"], "STUDENT")

    def test_scheduled_invitee_must_accept(self):
        invite = GroupSessionInvite.objects.create(
            session=self.session, user=self.guest, status="pending",
        )
        self.assertEqual(self._join().status_code, 403)
        invite.status = "accepted"
        invite.save(update_fields=["status"])
        self.assertEqual(self._join().status_code, 200)

    def test_instant_duration_selection_is_independent(self):
        self.assertEqual(INSTANT_COLLABORATION.duration_for_create(180), 180)
        self.assertEqual(INSTANT_COLLABORATION.duration_for_create("bad"), 60)
        self.assertEqual(INSTANT_COLLABORATION.duration_for_create(999), 60)

        policy = policy_for(self.session)
        self.assertEqual(
            policy.deadline(self.session),
            self.session.room_started_at + timedelta(minutes=60),
        )
        self.session.cap_ends_at = self.session.room_started_at + timedelta(minutes=90)
        self.assertEqual(policy.deadline(self.session), self.session.cap_ends_at)
