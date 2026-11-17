"""RC-006b — the ws-ticket route, the event socket, and the event envelope.

See `docs/architecture/shiksha-remote/tasks/phase-1/RC-006.md` and
`realtime-event-contracts.md` §Backend notification envelope.

Three things about how these tests are built, each of which is the difference
between testing something and testing nothing:

🔴 **The socket tests are `TransactionTestCase`.** The consumer reaches the
database through `database_sync_to_async` (the feature-flag read), i.e. on a
different thread, which cannot see an outer atomic block's uncommitted rows.
A plain `TestCase` makes `feature_enabled()` return the *default* False and
every socket closes 4404 — a suite that passes for the wrong reason.

🔴 **The refusals assert a close CODE, not merely "not connected".** A socket
that was rejected for the right reason and one rejected because a fixture was
malformed look identical from `connected == False`. Where the code is the
claim, the code is asserted.

🔴 **The negative tests here were mutation-checked.** Each one was re-run
against a deliberately broken implementation to prove it goes red; the
mapping is recorded in `evidence/collaboration-socket.md`. This project has
now been bitten twice by negative tests that passed vacuously (RC-031's
path-traversal assertions, RC-006a's "evil origin"), so "the suite is green"
is not evidence until it has been shown to go red.
"""
import asyncio
import json
from unittest import mock

from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from accounts.models import User
from global_settings.models import GlobalSettings
from sessions_app.collaboration import bindings as binding_store
from sessions_app.collaboration import consumers as collab_consumers
from sessions_app.collaboration import events, policy
from sessions_app.collaboration.consumers import CollaborationEventConsumer
from sessions_app.models import GroupSession, GroupSessionParticipant

from .tests_collaboration_policy import (
    BASE,
    EVIL_PREFIX_ORIGIN,
    EVIL_SUFFIX_ORIGIN,
    ORIGIN,
    CollaborationTestBase,
)

WS_PATH = "/ws/collaboration/v1/"


# ══════════════════════════════════════════════════════════════════════════
# POST ws-ticket/
# ══════════════════════════════════════════════════════════════════════════


class WsTicketRouteTest(CollaborationTestBase):
    """The HTTP half: who may mint a socket ticket, and on what terms."""

    def setUp(self):
        super().setUp()
        self.client_s = self.client_for(self.student)
        res = self.bind(self.client_s, self.student)
        assert res.status_code == 201, res.content
        self.binding_id = res.json()["binding_id"]

    def _mint(self, body=None, **kwargs):
        if body is None:
            body = {"binding_id": self.binding_id}
        return self.post(self.client_s, "/ws-ticket/", body, **kwargs)

    def test_a_bound_participant_gets_a_ticket(self):
        res = self._mint()
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertEqual(sorted(data), ["expires_in", "ticket", "ws_url"])
        self.assertEqual(data["expires_in"], binding_store.TICKET_TTL_SECONDS)
        self.assertEqual(data["expires_in"], 30)  # the contract's literal
        self.assertTrue(data["ticket"])
        self.assertTrue(data["ws_url"].endswith(WS_PATH))
        self.assertEqual(res["Cache-Control"], "no-store")

    def test_the_minted_ticket_resolves_to_this_binding(self):
        ticket = self._mint().json()["ticket"]
        claims = binding_store.consume_ticket(ticket)
        self.assertEqual(claims["binding_id"], self.binding_id)
        self.assertEqual(claims["user_id"], str(self.student.id))

    def test_sending_both_ids_is_refused(self):
        """'not both' — an ambiguous request must not be resolved by picking
        one, because the two carry different authority."""
        res = self._mint({
            "binding_id": self.binding_id,
            "host_binding_id": "11111111-1111-4111-8111-111111111111",
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "invalid_body")

    def test_sending_neither_id_is_refused(self):
        res = self._mint({})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "missing_fields")

    def test_a_device_ticket_is_not_yet_available(self):
        """422 'not yet', deliberately distinguishable from 400 'you made that
        up' — a client built against the full contract deserves the former."""
        res = self._mint({"host_binding_id": "11111111-1111-4111-8111-111111111111"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["code"], "unsupported_capability")

    def test_a_stranger_cannot_mint_for_someone_elses_binding(self):
        other = self.client_for(self.student2)
        res = self.post(other, "/ws-ticket/", {"binding_id": self.binding_id})
        self.assertEqual(res.status_code, 404)

    def test_an_unknown_binding_is_404(self):
        res = self._mint({"binding_id": "11111111-1111-4111-8111-111111111111"})
        self.assertEqual(res.status_code, 404)

    def test_a_lapsed_binding_cannot_mint(self):
        binding_store.delete(self.binding_id)
        self.assertEqual(self._mint().status_code, 404)

    def test_unknown_fields_are_rejected(self):
        res = self._mint({"binding_id": self.binding_id, "scope": "everything"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "unknown_fields")

    def test_an_evil_origin_is_refused(self):
        for evil in (EVIL_SUFFIX_ORIGIN, EVIL_PREFIX_ORIGIN):
            with self.subTest(origin=evil):
                res = self._mint(origin=evil)
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.json()["code"], "origin_forbidden")

    def test_a_missing_csrf_token_is_refused(self):
        res = self._mint(csrf=None)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "csrf_failed")

    def test_a_missing_idempotency_key_is_refused(self):
        res = self._mint(idem=None)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "idempotency_key_required")

    def test_the_route_is_404_while_the_flag_is_off(self):
        # The CSRF token has to be bootstrapped BEFORE the flag goes off:
        # `csrf/` is gated by the same flag, so fetching one afterwards 404s
        # and the test would never reach the route it means to exercise.
        token = self.csrf(self.client_s)
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = False
        s.save()
        res = self._mint(csrf=token)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], "not_found")

    def test_replaying_the_same_key_returns_the_same_ticket(self):
        """Correct idempotency, and a documented wart: the replayed ticket may
        already be spent. A client needing a fresh one must send a fresh key.
        """
        first = self._mint().json()["ticket"]
        second = self._mint().json()["ticket"]
        self.assertEqual(first, second)

    def test_a_fresh_key_mints_a_distinct_ticket(self):
        first = self._mint().json()["ticket"]
        second = self._mint(idem="99999999-8888-4777-8666-555555555555").json()["ticket"]
        self.assertNotEqual(first, second)

    def test_the_ws_url_matches_the_request_scheme_and_host(self):
        res = self._mint()
        self.assertEqual(res.json()["ws_url"], f"ws://testserver{WS_PATH}")

    @override_settings(COLLABORATION_WS_URL="wss://edge.example.test/ws/collaboration/v1/")
    def test_an_explicit_ws_url_setting_wins(self):
        res = self._mint()
        self.assertEqual(
            res.json()["ws_url"], "wss://edge.example.test/ws/collaboration/v1/"
        )


# ══════════════════════════════════════════════════════════════════════════
# /ws/collaboration/v1/
# ══════════════════════════════════════════════════════════════════════════


@override_settings(COLLABORATION_ALLOWED_ORIGINS=[ORIGIN])
class CollaborationSocketTestBase(TransactionTestCase):
    """One real binding, one real ticket, one socket per test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = True
        s.save()

        self.user = User.objects.create_user(
            username="rc6bws", email="rc6bws@test.com", password="x")
        self.other = User.objects.create_user(
            username="rc6bws2", email="rc6bws2@test.com", password="x")
        self.session = GroupSession.objects.create(
            host=self.user, subject_name="Physics", course_title="C10",
            topic="Topic", scheduled_date=timezone.now().date(),
            scheduled_time=timezone.now().time(), duration_minutes=45,
            status="live", session_type="instant", short_code="RC006B",
            room_name="rc006-room-b")
        GroupSessionParticipant.objects.create(
            session=self.session, user=self.user)

        self.binding_id, self.record = self._make_binding()
        self.ticket = binding_store.mint_ticket(self.binding_id, self.record)

    def _make_binding(self, user=None, context="learner", auth_sid="sess-1"):
        user = user or self.user
        principal = policy.Principal(
            user=user, auth_sid=auth_sid, context=context, profile_id="p1")
        return binding_store.create(
            principal, self.session, f"PA_{user.username}",
            policy.livekit_identity(user, self.session),
            policy.ALL_CAPABILITIES,
        )

    # ── socket plumbing ───────────────────────────────────────────────────

    def communicator(self, origin=ORIGIN, user=None):
        headers = []
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        comm = WebsocketCommunicator(
            CollaborationEventConsumer.as_asgi(), WS_PATH, headers=headers)
        comm.scope["user"] = user
        return comm

    @staticmethod
    async def authenticate(comm, ticket):
        await comm.send_to(text_data=json.dumps(
            {"v": 1, "type": "authenticate", "ticket": ticket}))

    @staticmethod
    async def close_code(comm, limit=40):
        """The application close code, or None if the socket stayed open."""
        for _ in range(limit):
            try:
                message = await comm.receive_output(timeout=2)
            except asyncio.TimeoutError:
                return None
            if message["type"] == "websocket.close":
                return message.get("code")
        return None

    @staticmethod
    async def cleanup(comm):
        """Best-effort teardown.

        🔴 Not a swallowed assertion — a swallowed *harness* race. asgiref's
        `ApplicationCommunicator.receive_output` cancels the application
        future on its timeout path, and `send_input` then re-raises that as
        `CancelledError`, so a `disconnect()` issued after the consumer has
        already closed the socket can blow up in teardown. It surfaced as an
        intermittent error in exactly one test, which is the worst kind: a
        suite that is red on timing rather than on behaviour teaches people
        to re-run instead of to read.

        Every assertion in this module is made on the close CODE, captured
        by `close_code` before this ever runs.

        🔴 `asyncio.CancelledError` derives from **BaseException**, not
        Exception, so a bare `except Exception` does not catch it — which is
        exactly how the first attempt at this helper failed to fix anything.
        A genuine application crash still surfaces: asgiref re-raises the
        app's own exception, and only a harness cancellation arrives as
        CancelledError.
        """
        try:
            await comm.disconnect()
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 — teardown only
            pass

    def run_socket(self, coro_factory):
        """Run one async socket scenario and always disconnect."""
        async def runner():
            comm = self.communicator() if not hasattr(
                coro_factory, "_needs_comm") else None
            try:
                return await coro_factory()
            finally:
                if comm is not None:
                    await self.cleanup(comm)
        return async_to_sync(runner)()


class SocketHandshakeTest(CollaborationSocketTestBase):
    def test_a_valid_ticket_authenticates(self):
        async def run():
            comm = self.communicator()
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await self.authenticate(comm, self.ticket)
            ack = json.loads(await comm.receive_from(timeout=2))
            await self.cleanup(comm)
            return ack

        ack = async_to_sync(run)()
        self.assertEqual(ack["type"], "authenticated")
        self.assertEqual(ack["binding_id"], self.binding_id)
        self.assertEqual(ack["v"], 1)

    def test_a_ticket_is_single_use(self):
        async def run():
            first = self.communicator()
            await first.connect()
            await self.authenticate(first, self.ticket)
            await first.receive_from(timeout=2)   # the ack
            await self.cleanup(first)

            second = self.communicator()
            await second.connect()
            await self.authenticate(second, self.ticket)
            code = await self.close_code(second)
            await self.cleanup(second)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_UNAUTHENTICATED)

    def test_an_unknown_ticket_is_refused(self):
        async def run():
            comm = self.communicator()
            await comm.connect()
            await self.authenticate(comm, "not-a-real-ticket")
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_UNAUTHENTICATED)

    def test_a_ticket_whose_binding_has_lapsed_is_refused(self):
        """The ticket lives 30s and the binding 6s, so this is reachable in
        ordinary use — authority follows the binding, never the ticket."""
        binding_store.delete(self.binding_id)

        async def run():
            comm = self.communicator()
            await comm.connect()
            await self.authenticate(comm, self.ticket)
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_UNAUTHENTICATED)

    def test_a_ticket_replayed_from_another_accounts_browser_is_refused(self):
        """Defence in depth: the ticket is the contract's authentication, but
        a handshake that DID carry a session cookie must agree with it."""
        async def run():
            comm = self.communicator(user=self.other)
            await comm.connect()
            await self.authenticate(comm, self.ticket)
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_FORBIDDEN)

    def test_nothing_is_readable_before_authenticating(self):
        async def run():
            comm = self.communicator()
            await comm.connect()
            await comm.send_to(text_data=json.dumps({"v": 1, "type": "ping"}))
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_UNAUTHENTICATED)

    def test_an_unauthenticated_socket_expires(self):
        async def run():
            with mock.patch.object(
                collab_consumers, "AUTH_DEADLINE_SECONDS", 0.15
            ):
                comm = self.communicator()
                await comm.connect()
                code = await self.close_code(comm)
                await self.cleanup(comm)
                return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_AUTH_TIMEOUT)

    def test_an_authenticate_frame_with_extra_fields_is_refused(self):
        async def run():
            comm = self.communicator()
            await comm.connect()
            await comm.send_to(text_data=json.dumps({
                "v": 1, "type": "authenticate",
                "ticket": self.ticket, "capabilities": ["pointer.move"],
            }))
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_PROTOCOL_ERROR)

    def test_a_wrong_envelope_version_is_refused(self):
        async def run():
            comm = self.communicator()
            await comm.connect()
            await comm.send_to(text_data=json.dumps(
                {"v": 2, "type": "authenticate", "ticket": self.ticket}))
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_PROTOCOL_ERROR)

    def test_malformed_json_is_refused(self):
        async def run():
            comm = self.communicator()
            await comm.connect()
            await comm.send_to(text_data="{not json")
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_PROTOCOL_ERROR)


class SocketOriginTest(CollaborationSocketTestBase):
    """The hole this consumer refuses to inherit.

    `config/asgi.py` wraps the websocket stack in JWTAuthMiddleware and NO
    OriginValidator, and cookies are SameSite=None on a shared parent domain
    — so any origin can already open an authenticated socket to the existing
    chat consumers. That is pre-existing and repo-wide; this route checks for
    itself.
    """

    def test_a_lookalike_suffix_origin_is_refused_at_the_handshake(self):
        async def run():
            comm = self.communicator(origin=EVIL_SUFFIX_ORIGIN)
            connected, code = await comm.connect()
            await self.cleanup(comm)
            return connected, code

        connected, code = async_to_sync(run)()
        self.assertFalse(connected)
        self.assertEqual(code, collab_consumers.CLOSE_FORBIDDEN)

    def test_a_lookalike_prefix_origin_is_refused_at_the_handshake(self):
        async def run():
            comm = self.communicator(origin=EVIL_PREFIX_ORIGIN)
            connected, code = await comm.connect()
            await self.cleanup(comm)
            return connected, code

        connected, code = async_to_sync(run)()
        self.assertFalse(connected)
        self.assertEqual(code, collab_consumers.CLOSE_FORBIDDEN)

    def test_a_missing_origin_cannot_bypass_browser_authentication(self):
        """The contract lets the NATIVE route omit Origin. No device ticket
        can exist until RC-007, so a browser ticket presented without an
        Origin is refused — at authenticate time, so RC-007 can land the
        device case without reopening the handshake.
        """
        async def run():
            comm = self.communicator(origin=None)
            connected, _ = await comm.connect()
            self.assertTrue(connected)  # handshake survives; the ticket decides
            await self.authenticate(comm, self.ticket)
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_FORBIDDEN)


class SocketVocabularyTest(CollaborationSocketTestBase):
    """'Pings/invalidation only; REST remains authoritative.'"""

    async def _authenticated(self):
        """A fresh ticket every time, deliberately.

        Tickets are single-use, so a helper that reused `self.ticket` would
        authenticate once and then hand every later socket a dead credential
        — which presents as the test's own assertion failing for an unrelated
        reason. (This is not hypothetical: the first draft of
        `test_an_input_frame_closes_the_socket` did exactly that across its
        four subTests, and three of them failed on the stale ticket rather
        than on the frame under test.)
        """
        ticket = await sync_to_async(binding_store.mint_ticket)(
            self.binding_id, self.record)
        comm = self.communicator()
        await comm.connect()
        await self.authenticate(comm, ticket)
        await comm.receive_from(timeout=2)  # ack
        return comm

    def test_the_ack_frames_match_the_ratified_contract(self):
        """`realtime-event-contracts.md` §Client→server frames (ratified
        17 Nov 2026) fixes both acks exactly. Pinned here because they were
        an *addition* to the pack — if the doc and the wire ever disagree,
        this is the test that says which one moved.

        They must carry no authority: `binding_id` only echoes what the
        client already presented, and `expires_in` only restates the TTL.
        """
        async def run():
            ticket = await sync_to_async(binding_store.mint_ticket)(
                self.binding_id, self.record)
            comm = self.communicator()
            await comm.connect()
            await self.authenticate(comm, ticket)
            ack = json.loads(await comm.receive_from(timeout=2))
            await comm.send_to(text_data=json.dumps({"v": 1, "type": "ping"}))
            pong = json.loads(await comm.receive_from(timeout=2))
            await self.cleanup(comm)
            return ack, pong

        ack, pong = async_to_sync(run)()
        self.assertEqual(sorted(ack), ["binding_id", "expires_in", "type", "v"])
        self.assertEqual(ack["v"], 1)
        self.assertEqual(ack["type"], "authenticated")
        self.assertEqual(ack["binding_id"], self.binding_id)
        self.assertEqual(ack["expires_in"], binding_store.BINDING_TTL_SECONDS)

        self.assertEqual(sorted(pong), ["server_time", "type", "v"])
        self.assertEqual(pong["type"], "pong")

        # No authority leaks into either ack.
        blob = (json.dumps(ack) + json.dumps(pong)).lower()
        for forbidden in ("ticket", "token", "jwt", "capab", "secret", "auth_sid"):
            self.assertNotIn(forbidden, blob)

    def test_the_close_codes_match_the_ratified_contract(self):
        """The doc lists these by number; a renumbering would silently break
        every client's error handling."""
        self.assertEqual(
            {
                collab_consumers.CLOSE_PROTOCOL_ERROR,
                collab_consumers.CLOSE_UNAUTHENTICATED,
                collab_consumers.CLOSE_FORBIDDEN,
                collab_consumers.CLOSE_FEATURE_OFF,
                collab_consumers.CLOSE_AUTH_TIMEOUT,
                collab_consumers.CLOSE_FRAME_TOO_LARGE,
                collab_consumers.CLOSE_RATE_LIMITED,
                collab_consumers.CLOSE_DEPENDENCY_UNAVAILABLE,
            },
            {4400, 4401, 4403, 4404, 4408, 4413, 4429, 4503},
        )

    def test_a_ping_is_answered(self):
        async def run():
            comm = await self._authenticated()
            await comm.send_to(text_data=json.dumps({"v": 1, "type": "ping"}))
            pong = json.loads(await comm.receive_from(timeout=2))
            await self.cleanup(comm)
            return pong

        self.assertEqual(async_to_sync(run)()["type"], "pong")

    def test_an_input_frame_closes_the_socket(self):
        """🔴 The refusal that matters most. Input rides the LiveKit data
        channel under ADR-02 Option C; this socket has no inbound verb for
        pointer, keyboard, consent or grant mutation. If this test ever goes
        green against a consumer that ACCEPTS the frame, a new authorization
        surface has been opened without a contract decision.
        """
        for frame in (
            {"v": 1, "type": "input", "x": 0.5, "y": 0.5},
            {"v": 1, "type": "pointer.move", "x": 0.5, "y": 0.5},
            {"v": 1, "type": "grant.decision", "allow": True},
            {"v": 1, "type": "subscribe", "room": "someone-elses-room"},
        ):
            with self.subTest(frame=frame["type"]):
                async def run():
                    comm = await self._authenticated()
                    await comm.send_to(text_data=json.dumps(frame))
                    code = await self.close_code(comm)
                    await self.cleanup(comm)
                    return code

                self.assertEqual(
                    async_to_sync(run)(), collab_consumers.CLOSE_PROTOCOL_ERROR)

    def test_an_oversized_frame_is_refused(self):
        async def run():
            comm = await self._authenticated()
            await comm.send_to(text_data=json.dumps(
                {"v": 1, "type": "ping", "pad": "A" * (8 * 1024)}))
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_FRAME_TOO_LARGE)

    def test_the_size_ceiling_counts_bytes_not_characters(self):
        """A multi-byte payload passes a naive len() ceiling at roughly a
        quarter of its real size. '🔴' is 4 bytes, 1 character.

        🔴 **This test was VACUOUS on first writing and the mutation sweep
        caught it — the third such case in this project.** It built the frame
        with a plain `json.dumps`, whose default `ensure_ascii=True` escapes
        every '🔴' to `\\ud83d\\udd34` — twelve ASCII characters. The frame
        was therefore 25,235 *characters*, over the 8 KiB ceiling by either
        measure, so a consumer counting characters rejected it too and the
        test passed against the very bug it was written to catch.

        `ensure_ascii=False` is the whole point: it keeps the payload
        multi-byte on the wire, giving 2,135 characters (under the ceiling)
        and 8,435 bytes (over it). The assertions below pin that gap so the
        frame can never silently drift back to being over on both counts.
        """
        padding = "🔴" * 2100
        frame = json.dumps(
            {"v": 1, "type": "ping", "pad": padding}, ensure_ascii=False)
        self.assertLessEqual(len(frame), collab_consumers.MAX_FRAME_BYTES)
        self.assertGreater(
            len(frame.encode("utf-8")), collab_consumers.MAX_FRAME_BYTES)

        async def run():
            comm = await self._authenticated()
            await comm.send_to(text_data=frame)
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_FRAME_TOO_LARGE)

    def test_a_binary_frame_is_refused(self):
        async def run():
            comm = await self._authenticated()
            await comm.send_to(bytes_data=b"\x00\x01\x02")
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_PROTOCOL_ERROR)

    def test_a_frame_flood_is_rate_limited(self):
        async def run():
            comm = await self._authenticated()
            for _ in range(collab_consumers.MAX_FRAMES_PER_SECOND + 5):
                await comm.send_to(text_data=json.dumps({"v": 1, "type": "ping"}))
            code = await self.close_code(comm, limit=80)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_RATE_LIMITED)

    def test_a_ping_drops_the_socket_once_the_binding_lapses(self):
        """The socket cannot outlive its binding by more than one ping."""
        async def run():
            comm = await self._authenticated()
            await sync_to_async(binding_store.delete)(self.binding_id)
            await comm.send_to(text_data=json.dumps({"v": 1, "type": "ping"}))
            code = await self.close_code(comm)
            await self.cleanup(comm)
            return code

        self.assertEqual(async_to_sync(run)(), collab_consumers.CLOSE_UNAUTHENTICATED)


class SocketFeatureFlagTest(CollaborationSocketTestBase):
    def test_the_socket_is_closed_while_the_flag_is_off(self):
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = False
        s.save()

        async def run():
            comm = self.communicator()
            connected, code = await comm.connect()
            await self.cleanup(comm)
            return connected, code

        connected, code = async_to_sync(run)()
        self.assertFalse(connected)
        self.assertEqual(code, collab_consumers.CLOSE_FEATURE_OFF)

    def test_the_legacy_flag_does_not_turn_the_socket_on(self):
        """`live_remote_access_enabled` defaults True and gates the DORMANT
        legacy path. It must not reach this feature."""
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = False
        s.live_remote_access_enabled = True
        s.save()

        async def run():
            comm = self.communicator()
            connected, code = await comm.connect()
            await self.cleanup(comm)
            return connected, code

        connected, code = async_to_sync(run)()
        self.assertFalse(connected)
        self.assertEqual(code, collab_consumers.CLOSE_FEATURE_OFF)


class SocketAttendanceTest(CollaborationSocketTestBase):
    """🔴 This socket must not be mistaken for presence.

    `GroupSessionChatConsumer` increments `active_connections`, which
    group_session_views uses as the room-capacity gate. A control socket that
    counted would consume a seat and push genuine invitees into 409
    room_full — and it would do it invisibly, because the control UI opens
    alongside the chat socket rather than instead of it.
    """

    def test_the_socket_never_touches_the_attendance_counter(self):
        before = GroupSession.objects.get(pk=self.session.pk).active_connections

        async def run():
            comm = self.communicator()
            await comm.connect()
            await self.authenticate(comm, self.ticket)
            await comm.receive_from(timeout=2)
            during = await sync_to_async(
                lambda: GroupSession.objects.get(pk=self.session.pk).active_connections
            )()
            await self.cleanup(comm)
            return during

        during = async_to_sync(run)()
        after = GroupSession.objects.get(pk=self.session.pk).active_connections
        self.assertEqual(before, during)
        self.assertEqual(before, after)

    def test_no_participant_row_is_created_by_connecting(self):
        before = GroupSessionParticipant.objects.filter(session=self.session).count()

        async def run():
            comm = self.communicator()
            await comm.connect()
            await self.authenticate(comm, self.ticket)
            await comm.receive_from(timeout=2)
            await self.cleanup(comm)

        async_to_sync(run)()
        self.assertEqual(
            GroupSessionParticipant.objects.filter(session=self.session).count(),
            before,
        )


class SocketDeliveryTest(CollaborationSocketTestBase):
    """Events reach the binding they are addressed to, and nothing wider."""

    async def _authenticated(self):
        """Fresh ticket per socket — see SocketVocabularyTest._authenticated."""
        ticket = await sync_to_async(binding_store.mint_ticket)(
            self.binding_id, self.record)
        comm = self.communicator()
        await comm.connect()
        await self.authenticate(comm, ticket)
        await comm.receive_from(timeout=2)
        return comm

    def test_an_event_addressed_to_the_binding_arrives(self):
        async def run():
            comm = await self._authenticated()
            layer = get_channel_layer()
            await layer.group_send(
                events.binding_group(self.binding_id),
                {"type": "collaboration.event",
                 "envelope": events.envelope("grant.changed", "grant-1", 3)},
            )
            received = json.loads(await comm.receive_from(timeout=2))
            await self.cleanup(comm)
            return received

        received = async_to_sync(run)()
        self.assertEqual(received["type"], "grant.changed")
        self.assertEqual(received["resource_id"], "grant-1")
        self.assertEqual(received["version"], 3)
        self.assertEqual(received["v"], 1)
        self.assertIn("event_id", received)

    def test_an_event_addressed_to_the_principal_arrives(self):
        async def run():
            comm = await self._authenticated()
            layer = get_channel_layer()
            await layer.group_send(
                events.principal_group(str(self.user.id), "learner"),
                {"type": "collaboration.event",
                 "envelope": events.envelope("session.ended", str(self.session.id), 1)},
            )
            received = json.loads(await comm.receive_from(timeout=2))
            await self.cleanup(comm)
            return received

        self.assertEqual(async_to_sync(run)()["type"], "session.ended")

    def test_an_event_for_a_different_binding_does_not_arrive(self):
        other_binding, _ = self._make_binding(user=self.other)

        async def run():
            comm = await self._authenticated()
            layer = get_channel_layer()
            await layer.group_send(
                events.binding_group(other_binding),
                {"type": "collaboration.event",
                 "envelope": events.envelope("grant.changed", "grant-2", 1)},
            )
            nothing = await comm.receive_nothing(timeout=0.5)
            await self.cleanup(comm)
            return nothing

        self.assertTrue(async_to_sync(run)())

    def test_an_event_for_the_same_account_in_another_context_does_not_arrive(self):
        """A learner profile and a teaching profile on one account are
        different principals here, exactly as `bindings.owned_by` says."""
        async def run():
            comm = await self._authenticated()
            layer = get_channel_layer()
            await layer.group_send(
                events.principal_group(str(self.user.id), "teacher"),
                {"type": "collaboration.event",
                 "envelope": events.envelope("grant.changed", "grant-3", 1)},
            )
            nothing = await comm.receive_nothing(timeout=0.5)
            await self.cleanup(comm)
            return nothing

        self.assertTrue(async_to_sync(run)())

    def test_a_disconnected_socket_stops_receiving(self):
        async def run():
            comm = await self._authenticated()
            await self.cleanup(comm)
            layer = get_channel_layer()
            # Must not raise, and must reach nobody.
            await layer.group_send(
                events.binding_group(self.binding_id),
                {"type": "collaboration.event",
                 "envelope": events.envelope("grant.changed", "grant-4", 1)},
            )

        async_to_sync(run)()  # the assertion is that this does not hang or raise


# ══════════════════════════════════════════════════════════════════════════
# The envelope itself
# ══════════════════════════════════════════════════════════════════════════


class EventEnvelopeTest(SimpleTestCase):
    def test_the_envelope_shape_is_the_contract(self):
        body = events.envelope("transfer.changed", "t-1", 7)
        self.assertEqual(
            sorted(body), ["event_id", "resource_id", "type", "v", "version"])
        self.assertEqual(body["v"], 1)
        self.assertEqual(body["version"], 7)

    def test_the_five_event_types_are_exactly_the_contract(self):
        self.assertEqual(
            events.EVENT_TYPES,
            {"grant.changed", "device.changed", "host_binding.changed",
             "transfer.changed", "session.ended"},
        )

    def test_an_unknown_event_type_raises(self):
        """A typo'd type is a notification the client ignores forever, which
        presents as 'the UI just doesn't update sometimes'."""
        with self.assertRaises(ValueError):
            events.envelope("grant.updated", "g-1", 1)

    def test_a_boolean_version_raises(self):
        """bool is an int subclass, and True as a version breaks the client's
        'accept only increasing version' rule in ways that are hard to see."""
        with self.assertRaises(ValueError):
            events.envelope("grant.changed", "g-1", True)

    def test_a_non_integer_version_raises(self):
        with self.assertRaises(ValueError):
            events.envelope("grant.changed", "g-1", "3")

    def test_an_empty_resource_id_raises(self):
        with self.assertRaises(ValueError):
            events.envelope("grant.changed", "", 1)

    def test_the_envelope_carries_no_payload(self):
        """An event is not authority: no tokens, filenames, capabilities or
        grant bodies. The client's only correct reaction is to refetch."""
        body = events.envelope("transfer.changed", "t-1", 1)
        serialized = json.dumps(body)
        for forbidden in ("token", "ticket", "capab", "filename", "name", "jwt"):
            self.assertNotIn(forbidden, serialized.lower())


class EventAddressingTest(SimpleTestCase):
    """`publish` refuses to send anything it cannot address."""

    class _RecordingLayer:
        def __init__(self):
            self.sent = []

        async def group_send(self, group, message):
            self.sent.append((group, message))

    def _publish(self, **kwargs):
        layer = self._RecordingLayer()
        with mock.patch.object(events, "get_channel_layer", return_value=layer):
            events.publish("grant.changed", "g-1", 2, **kwargs)
        return layer.sent

    def test_an_unaddressed_event_raises_rather_than_broadcasting(self):
        """🔴 The failure this module exists to prevent: 'no input, tokens,
        filenames or grants in broad room groups'. There is no room fan-out to
        fall back to, so publishing without a recipient must raise."""
        with self.assertRaises(ValueError):
            self._publish()

    def test_a_binding_addressed_event_goes_to_exactly_one_group(self):
        sent = self._publish(binding_id="b-1")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "collab.b.b-1")

    def test_a_principal_addressed_event_includes_the_context(self):
        sent = self._publish(user_id="u-1", context="learner")
        self.assertEqual(sent[0][0], "collab.u.u-1.learner")

    def test_a_contextless_principal_does_not_collide_with_a_real_context(self):
        bare = self._publish(user_id="u-1")[0][0]
        learner = self._publish(user_id="u-1", context="learner")[0][0]
        self.assertNotEqual(bare, learner)

    def test_group_names_survive_characters_channels_rejects(self):
        """Channels requires [a-zA-Z0-9_.-] under 100 chars and raises deep
        inside the layer at send time — the wrong place to find out."""
        import re

        name = events.principal_group("u/1 2", "learner:teacher")
        self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_.-]+", name), name)
        self.assertLess(len(name), 100)

    def test_publishing_validates_before_addressing(self):
        with self.assertRaises(ValueError):
            layer = self._RecordingLayer()
            with mock.patch.object(events, "get_channel_layer", return_value=layer):
                events.publish("grant.exploded", "g-1", 1, binding_id="b-1")
