# M3 §10 — end-to-end confirmation that the new "policy" error category
# (added to post_message_checked()) actually reaches a real client over a
# real WebSocket, through the real ChatConsumer — not just that
# services.post_message_checked() returns the right tuple when called
# directly (test_m3_policy.py already covers that in isolation). This is
# what chat/consumers.py's own module docstring now documents; this test
# is what backs that documentation with something that actually runs it.
#
# TransactionTestCase (not TestCase), per Channels' own testing guidance:
# the consumer's DB access goes through database_sync_to_async, which runs
# on a different thread than the test method itself — a bare TestCase's
# open-transaction-per-test wrapping doesn't reliably hand data across
# that thread boundary the way a real (flushed-between-tests) DB does.
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase

from chat.consumers import ChatConsumer

from .factories import make_direct_conversation


class ChatConsumerPolicyIntegrationTest(TransactionTestCase):

    def test_frozen_conversation_sends_a_policy_error_over_the_socket(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        conv.is_frozen = True
        conv.save(update_fields=["is_frozen"])

        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)

        async def run():
            communicator = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            # Bypassing JWTAuthMiddleware/URLRouter (out of scope here —
            # this test is about the policy gate, not auth), so the scope
            # bits they'd normally populate are set directly.
            communicator.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            communicator.scope["user"] = learner_account
            communicator.scope["context"] = "learner"
            communicator.scope["active_profile_id"] = learner_profile_id
            communicator.scope["identity"] = None

            connected, _ = await communicator.connect()
            assert connected, "consumer refused the connection"

            await communicator.receive_json_from()  # the initial "history" frame

            await communicator.send_json_to({"type": "message", "body": "hello?"})
            response = await communicator.receive_json_from()

            await communicator.disconnect()
            return response

        response = async_to_sync(run)()
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["data"]["category"], "policy")

    def test_ordinary_conversation_still_delivers_the_message(self):
        """Sanity check alongside the refusal case above: an unfrozen
        conversation must still work exactly as before — the new gate
        adds a refusal path, it doesn't add friction to the normal one."""
        conv, learner_p, teacher_p = make_direct_conversation()
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)

        async def run():
            communicator = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            communicator.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            communicator.scope["user"] = learner_account
            communicator.scope["context"] = "learner"
            communicator.scope["active_profile_id"] = learner_profile_id
            communicator.scope["identity"] = None

            connected, _ = await communicator.connect()
            assert connected

            await communicator.receive_json_from()  # history

            await communicator.send_json_to({"type": "message", "body": "hi teacher"})
            response = await communicator.receive_json_from()

            await communicator.disconnect()
            return response

        response = async_to_sync(run)()
        self.assertEqual(response["type"], "message")
        self.assertEqual(response["data"]["body"], "hi teacher")
