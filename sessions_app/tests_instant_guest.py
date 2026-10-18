"""Anonymous collaboration guests never become account or group members."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from .models import GroupSession, InstantCollaborationGuest


class InstantGuestTests(TestCase):
    def setUp(self):
        self.host = User.objects.create_user(
            username="guest_flow_host", email="guest_flow_host@test.com", password="pw"
        )
        now = timezone.now()
        self.session = GroupSession.objects.create(
            host=self.host, topic="Study together",
            scheduled_date=timezone.localdate(),
            scheduled_time=timezone.localtime().time().replace(microsecond=0),
            duration_minutes=60, session_type="instant", status="live",
            room_started_at=now, room_name="guest_flow_room",
            short_code="abc-defg-hij", admit_mode="lobby",
        )
        self.anon = APIClient()
        self.host_client = APIClient()
        self.host_client.force_authenticate(user=self.host)

    def _request(self, code="abc-defg-hij"):
        return self.anon.post(
            "/api/sessions/group-sessions/guest/request/",
            {"code": code, "name": "Alex Guest"}, format="json",
        )

    def test_guest_needs_approval_and_removal_revokes_new_tokens(self):
        asked = self._request()
        self.assertEqual(asked.status_code, 202)
        guest_id = InstantCollaborationGuest.objects.get(session=self.session).id
        credential = asked.data["credential"]
        base = f"/api/sessions/group-sessions/{self.session.id}"
        self.assertEqual(
            self.anon.post(f"{base}/guest/join/", {"credential": credential}).status_code,
            403,
        )
        listed = self.host_client.get(f"{base}/join-requests/")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.data[0]["guest"])
        self.assertEqual(listed.data[0]["name"], "Alex Guest")
        admitted = self.host_client.post(f"{base}/join-requests/{guest_id}/admit/")
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(
            self.anon.post(f"{base}/guest/status/", {"credential": credential}).data["status"],
            "admitted",
        )
        with patch("sessions_app.instant_guest_views._guest_token", return_value="livekit-token"):
            joined = self.anon.post(f"{base}/guest/join/", {"credential": credential})
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(joined.data["token"], "livekit-token")
        with patch("livestream.services.room_admin.remove_participant") as remove:
            removed = self.host_client.post(f"{base}/guest/{guest_id}/remove/")
        self.assertEqual(removed.status_code, 200)
        remove.assert_called_once_with("guest_flow_room", f"guest_{guest_id}")
        self.assertEqual(
            self.anon.post(f"{base}/guest/status/", {"credential": credential}).data["status"],
            "removed",
        )
        self.assertEqual(
            self.anon.post(f"{base}/guest/join/", {"credential": credential}).status_code,
            403,
        )

    def test_scheduled_room_cannot_be_joined_as_guest(self):
        self.session.session_type = "scheduled"
        self.session.save(update_fields=["session_type"])
        self.assertEqual(self._request().status_code, 404)

    def test_guest_code_requests_are_rate_limited(self):
        with patch("sessions_app.instant_guest_views.GuestRequestThrottle.rate", "1/min"):
            first = self.anon.post(
                "/api/sessions/group-sessions/guest/request/",
                {"code": "abc-defg-hij", "name": "Rate Test"},
                format="json", HTTP_X_FORWARDED_FOR="198.51.100.42",
            )
            second = self.anon.post(
                "/api/sessions/group-sessions/guest/request/",
                {"code": "abc-defg-hij", "name": "Rate Test"},
                format="json", HTTP_X_FORWARDED_FOR="198.51.100.42",
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)

    def test_expired_room_and_invalid_pass_are_rejected(self):
        asked = self._request()
        base = f"/api/sessions/group-sessions/{self.session.id}"
        self.assertEqual(
            self.anon.post(f"{base}/guest/status/", {"credential": "forged"}).status_code,
            403,
        )
        self.session.room_started_at = timezone.now() - timedelta(hours=2)
        self.session.save(update_fields=["room_started_at"])
        self.assertEqual(
            self.anon.post(f"{base}/guest/status/", {"credential": asked.data["credential"]}).data["status"],
            "ended",
        )
        self.assertEqual(self._request().status_code, 404)
