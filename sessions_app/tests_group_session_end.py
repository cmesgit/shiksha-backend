"""
Tests for the host-ended group-session path (`end_group_session`).

Written alongside the fix for the audit finding "ending a group session
leaves the room open": the view wrote status/ended_at/all_left_at but never
called close_room() and never closed open GroupSessionParticipant rows,
unlike its own sibling _end_group_session_internal. The host confirmed
"Participants will be disconnected immediately", left, and everyone kept
talking while the card read Completed.

These do not need LiveKit credentials — close_room is patched at the point
of use, which is also what lets us assert it was called at all.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from .models import GroupSession, GroupSessionChatMessage, GroupSessionParticipant


class EndGroupSessionTests(TestCase):
    """The host's End button must actually end the call, not just the row."""

    def setUp(self):
        self.host = User.objects.create_user(
            username="gs_host", email="gs_host@test.com", password="pw",
        )
        self.guest = User.objects.create_user(
            username="gs_guest", email="gs_guest@test.com", password="pw",
        )
        now = timezone.now()
        local_now = timezone.localtime(now)
        self.session = GroupSession.objects.create(
            host=self.host,
            topic="Doubt clearing",
            scheduled_date=local_now.date(),
            scheduled_time=local_now.time().replace(microsecond=0),
            duration_minutes=60,
            session_type="scheduled",
            status="live",
            room_started_at=now,
            active_connections=2,
        )
        self.session.room_name = f"group_session_{self.session.id}"
        self.session.save(update_fields=["room_name"])

        # Two people still in the room when the host pulls the plug.
        self.host_row = GroupSessionParticipant.objects.create(
            session=self.session, user=self.host,
        )
        self.guest_row = GroupSessionParticipant.objects.create(
            session=self.session, user=self.guest,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.host)
        self.url = reverse(
            "group-session-end", kwargs={"session_id": self.session.id},
        )

    def _end(self):
        """POST /end/ with close_room and the WS broadcast both stubbed."""
        with patch("livestream.services.room_admin.close_room") as close_room, \
             patch("sessions_app.group_session_views._broadcast_session_ended"):
            response = self.client.post(self.url)
        return response, close_room

    def test_end_closes_the_livekit_room(self):
        response, close_room = self._end()
        self.assertEqual(response.status_code, 200)
        close_room.assert_called_once_with(self.session.room_name)

    def test_end_closes_open_participant_rows(self):
        response, _ = self._end()
        self.assertEqual(response.status_code, 200)

        self.host_row.refresh_from_db()
        self.guest_row.refresh_from_db()
        self.session.refresh_from_db()

        # Both intervals must be closed, and stamped with the session's own
        # ended_at rather than left NULL — duration_seconds() reports 0 for
        # an open interval, so a NULL here makes a full meeting look unattended.
        self.assertIsNotNone(self.host_row.left_at)
        self.assertIsNotNone(self.guest_row.left_at)
        self.assertEqual(self.guest_row.left_at, self.session.ended_at)

    def test_end_still_writes_the_status_fields(self):
        """Guard the pre-existing behaviour the fix is layered on top of."""
        response, _ = self._end()
        self.assertEqual(response.status_code, 200)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "completed")
        self.assertIsNotNone(self.session.ended_at)
        self.assertIsNotNone(self.session.all_left_at)
        self.assertEqual(self.session.active_connections, 0)

    def test_a_room_that_never_started_does_not_call_close_room(self):
        """No room_name means there is no LiveKit room to close."""
        self.session.room_name = ""
        self.session.save(update_fields=["room_name"])

        response, close_room = self._end()
        self.assertEqual(response.status_code, 200)
        close_room.assert_not_called()

    def test_a_failing_close_room_does_not_fail_the_request(self):
        """LiveKit being unreachable must not strand the session as live."""
        with patch(
            "livestream.services.room_admin.close_room",
            side_effect=RuntimeError("livekit unreachable"),
        ), patch("sessions_app.group_session_views._broadcast_session_ended"):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "completed")

    def test_only_the_host_can_end(self):
        self.client.force_authenticate(user=self.guest)
        response, close_room = self._end()
        self.assertEqual(response.status_code, 403)
        close_room.assert_not_called()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "live")

    def test_ending_an_already_ended_session_is_a_no_op(self):
        self.session.status = "completed"
        self.session.save(update_fields=["status"])

        response, close_room = self._end()
        self.assertEqual(response.status_code, 200)
        close_room.assert_not_called()

    def test_instant_session_chat_is_still_purged(self):
        """The instant-only chat purge is pre-existing behaviour — keep it."""
        self.session.session_type = "instant"
        self.session.save(update_fields=["session_type"])
        GroupSessionChatMessage.objects.create(
            session=self.session,
            sender=self.host,
            sender_name="Host",
            sender_role="host",
            message="hello",
        )

        response, _ = self._end()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            GroupSessionChatMessage.objects.filter(session=self.session).count(), 0,
        )


class GroupSessionEndedBroadcastTests(TestCase):
    """`session_ended` must have a consumer handler to dispatch to.

    Channels resolves a group_send "type" to a method of the same name on
    the consumer and raises ValueError if there isn't one. _broadcast_session_ended
    has always sent this frame; the consumer had no handler, so every
    participant's chat socket died with a server error instead of an end
    signal — and the client's onclose then reconnected every 3 seconds.
    """

    def test_consumer_has_a_session_ended_handler(self):
        from .consumers import GroupSessionChatConsumer

        self.assertTrue(
            callable(getattr(GroupSessionChatConsumer, "session_ended", None)),
            "GroupSessionChatConsumer needs a session_ended handler or Channels "
            "raises 'No handler for message type session_ended'.",
        )

    def test_every_broadcast_type_has_a_matching_handler(self):
        """Catch the next one of these before it reaches production."""
        from .consumers import GroupSessionChatConsumer

        # The group_send "type" values aimed at group_session_chat_<id>.
        broadcast_types = [
            "chat_message",
            "session_ended",
            "session_extended",
            "session_file_added",
            "session_file_removed",
            "remote_control_requested",
        ]
        missing = [
            t for t in broadcast_types
            if not callable(getattr(GroupSessionChatConsumer, t, None))
        ]
        self.assertEqual(missing, [], f"consumer has no handler for: {missing}")
