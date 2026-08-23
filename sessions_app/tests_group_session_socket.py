"""
Auth gate on the session chat/presence WebSockets.

Written alongside the fix for the audit finding "the group-session chat
socket is unauthenticated": ``GroupSessionChatConsumer.connect()`` did a bare
``group_add`` + ``accept()``. It read ``scope["user"]`` (for the
remote-control routing) but never gated on it, so anyone who learned a
session UUID could open ``wss://…/ws/group-session/<uuid>/chat/`` with no
credentials, read every broadcast message, and — because this connection is
what increments ``active_connections``, the room-capacity gate in
group_session_views — push a room past its cap so genuine invitees got 409
``room_full``. ``PrivateSessionChatConsumer`` had the identical hole.

The tests bypass JWTAuthMiddleware/URLRouter and set ``scope["user"]``
directly, exactly as ``livestream.tests.CourseSessionConsumerAuthTests``
does: the subject here is the consumer's own gate, not token parsing.

To see these fail against the old code, stash the connect()/disconnect()
changes in sessions_app/consumers.py — every "rejected" assertion flips.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase
from django.utils import timezone

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator

from accounts.models import User

from .consumers import GroupSessionChatConsumer, PrivateSessionChatConsumer
from .models import (
    GroupSession,
    GroupSessionInvite,
    PrivateSession,
    SessionParticipant,
)


def _connect(consumer_cls, path, kwargs, user, while_open=None):
    """Open one socket against a consumer and report whether it was accepted.

    ``while_open`` runs (synchronously, off the event loop's DB thread) after
    the handshake resolves and before the disconnect, which is the only window
    in which ``active_connections`` reflects this socket.

    TransactionTestCase (not TestCase) because the consumer runs its DB work
    through ``database_sync_to_async``, i.e. on a different thread, which
    cannot see an outer atomic block's uncommitted rows.
    """
    from asgiref.sync import sync_to_async

    async def run():
        communicator = WebsocketCommunicator(consumer_cls.as_asgi(), path)
        communicator.scope["url_route"] = {"kwargs": kwargs}
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        if while_open is not None:
            await sync_to_async(while_open)()
        await communicator.disconnect()
        return connected

    return async_to_sync(run)()


class GroupSessionChatSocketAuthTests(TransactionTestCase):
    """Host / accepted invitee in, everyone else out — and the headcount
    must only move for sockets that were actually accepted."""

    def setUp(self):
        self.host = User.objects.create_user(
            username="gss_host", email="gss_host@test.com", password="pw",
        )
        self.invitee = User.objects.create_user(
            username="gss_inv", email="gss_inv@test.com", password="pw",
        )
        self.pending = User.objects.create_user(
            username="gss_pend", email="gss_pend@test.com", password="pw",
        )
        self.outsider = User.objects.create_user(
            username="gss_out", email="gss_out@test.com", password="pw",
        )

        now = timezone.localtime(timezone.now())
        self.session = GroupSession.objects.create(
            host=self.host,
            topic="Doubt clearing",
            scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60,
            session_type="scheduled",
            status="live",
            room_started_at=timezone.now(),
            active_connections=0,
        )
        GroupSessionInvite.objects.create(
            session=self.session, user=self.invitee, status="accepted",
        )
        GroupSessionInvite.objects.create(
            session=self.session, user=self.pending, status="pending",
        )

    def _connect(self, user, session_id=None, while_open=None):
        sid = str(session_id or self.session.id)
        return _connect(
            GroupSessionChatConsumer,
            f"/ws/group-session/{sid}/chat/",
            {"session_id": sid},
            user,
            while_open=while_open,
        )

    def test_anonymous_is_rejected(self):
        self.assertFalse(self._connect(AnonymousUser()))

    def test_outsider_is_rejected(self):
        self.assertFalse(self._connect(self.outsider))

    def test_invitee_who_has_not_accepted_is_rejected(self):
        # Mirrors join_group_session, which refuses a token for the same
        # reason ("You must accept the invite before you can join the room").
        self.assertFalse(self._connect(self.pending))

    def test_host_is_accepted(self):
        self.assertTrue(self._connect(self.host))

    def test_accepted_invitee_is_accepted(self):
        self.assertTrue(self._connect(self.invitee))

    def test_instant_meeting_is_open_to_any_authenticated_user(self):
        # Deliberate: an instant meeting is "anyone with the link", which is
        # exactly what join_group_session and the REST chat endpoints allow.
        # The gate that still matters there is *authenticated*.
        self.session.session_type = "instant"
        self.session.save(update_fields=["session_type"])
        self.assertTrue(self._connect(self.outsider))
        self.assertFalse(self._connect(AnonymousUser()))

    def test_unknown_session_id_is_rejected_not_a_500(self):
        self.assertFalse(
            self._connect(self.host, session_id="11111111-1111-1111-1111-111111111111")
        )

    def test_a_rejected_socket_never_counts_toward_room_capacity(self):
        """``active_connections`` is the room-capacity gate
        (INSTANT_MAX_PARTICIPANTS / GlobalSettings.live_max_participants), so
        an outsider's socket must not inflate it while it is open — that was
        the denial of service — nor deflate it on the way out, which would
        drive the count negative and auto-end a room full of people.
        """
        seen = []
        self._connect(
            self.outsider,
            while_open=lambda: seen.append(
                GroupSession.objects.get(pk=self.session.id).active_connections
            ),
        )
        self.assertEqual(seen, [0])

        self.session.refresh_from_db()
        self.assertEqual(self.session.active_connections, 0)

    def test_an_accepted_socket_does_count(self):
        """The counter still works — the gate above must not have broken it."""
        seen = []
        self._connect(
            self.host,
            while_open=lambda: seen.append(
                GroupSession.objects.get(pk=self.session.id).active_connections
            ),
        )
        self.assertEqual(seen, [1])

        self.session.refresh_from_db()
        self.assertEqual(self.session.active_connections, 0)


class PrivateSessionChatSocketAuthTests(TransactionTestCase):
    """Same gate on the 1-on-1 room: teacher, requester, accepted
    participant. Rule copied from views.session_chat_messages."""

    def setUp(self):
        self.teacher = User.objects.create_user(
            username="pss_t", email="pss_t@test.com", password="pw",
        )
        self.student = User.objects.create_user(
            username="pss_s", email="pss_s@test.com", password="pw",
        )
        self.classmate = User.objects.create_user(
            username="pss_c", email="pss_c@test.com", password="pw",
        )
        self.outsider = User.objects.create_user(
            username="pss_o", email="pss_o@test.com", password="pw",
        )

        now = timezone.localtime(timezone.now())
        self.session = PrivateSession.objects.create(
            teacher=self.teacher,
            requested_by=self.student,
            subject="Physics",
            scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60,
            status="ongoing",
            active_connections=0,
        )
        SessionParticipant.objects.create(
            session=self.session, user=self.classmate, status="accepted",
        )

    def _connect(self, user, while_open=None):
        sid = str(self.session.id)
        return _connect(
            PrivateSessionChatConsumer,
            f"/ws/private-session/{sid}/chat/",
            {"session_id": sid},
            user,
            while_open=while_open,
        )

    def test_anonymous_is_rejected(self):
        self.assertFalse(self._connect(AnonymousUser()))

    def test_outsider_is_rejected(self):
        self.assertFalse(self._connect(self.outsider))

    def test_teacher_is_accepted(self):
        self.assertTrue(self._connect(self.teacher))

    def test_requester_is_accepted(self):
        self.assertTrue(self._connect(self.student))

    def test_accepted_participant_is_accepted(self):
        self.assertTrue(self._connect(self.classmate))

    def test_a_rejected_socket_never_counts_toward_the_headcount(self):
        seen = []
        self._connect(
            self.outsider,
            while_open=lambda: seen.append(
                PrivateSession.objects.get(pk=self.session.id).active_connections
            ),
        )
        self.assertEqual(seen, [0])

        self.session.refresh_from_db()
        self.assertEqual(self.session.active_connections, 0)

    def test_an_accepted_socket_does_count(self):
        seen = []
        self._connect(
            self.teacher,
            while_open=lambda: seen.append(
                PrivateSession.objects.get(pk=self.session.id).active_connections
            ),
        )
        self.assertEqual(seen, [1])
