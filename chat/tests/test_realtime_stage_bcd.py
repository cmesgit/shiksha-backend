# Communication Center closure — realtime protocol additions, exercised over
# REAL WebSocket connections through the REAL ChatConsumer (not just unit
# tests of the underlying service functions — chat/tests/test_m3_consumer_
# integration.py established this pattern for the M3 policy gate; this file
# extends it to Stage B/C's new event types: reaction, message_deleted,
# read, and presence, plus confirming a message posted via the REST
# attachment path (no WS involved in the write) still reaches an open
# socket live — the whole point of centralizing the group_send in
# services._finalize_new_message() instead of leaving it in the consumer.
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase

from chat import services
from chat.consumers import ChatConsumer
from chat.models import Participant

from .factories import make_direct_conversation


def _scope_for(communicator, conv_id, account, context, active_profile_id=None):
    communicator.scope["url_route"] = {"kwargs": {"conversation_id": str(conv_id)}}
    communicator.scope["user"] = account
    communicator.scope["context"] = context
    communicator.scope["active_profile_id"] = active_profile_id
    communicator.scope["identity"] = None


class RealtimeProtocolIntegrationTest(TransactionTestCase):

    def test_both_sides_see_the_message_and_each_others_presence(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)
        teacher_account = teacher_p.teacher_profile.user

        async def run():
            learner_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(learner_ws, conv.id, learner_account, "learner", learner_profile_id)
            connected, _ = await learner_ws.connect()
            assert connected
            await learner_ws.receive_json_from()  # history

            teacher_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(teacher_ws, conv.id, teacher_account, "teacher")
            connected, _ = await teacher_ws.connect()
            assert connected
            await teacher_ws.receive_json_from()  # history

            # The learner, already connected, should see the teacher come online.
            presence = await learner_ws.receive_json_from()
            assert presence["type"] == "presence"
            assert presence["data"]["online"] is True

            await teacher_ws.send_json_to({"type": "message", "body": "hello from teacher"})
            learner_saw = await learner_ws.receive_json_from()
            teacher_saw = await teacher_ws.receive_json_from()  # the sender's own echo

            await learner_ws.disconnect()
            await teacher_ws.disconnect()
            return learner_saw, teacher_saw

        learner_saw, teacher_saw = async_to_sync(run)()
        self.assertEqual(learner_saw["type"], "message")
        self.assertEqual(learner_saw["data"]["body"], "hello from teacher")
        self.assertEqual(teacher_saw["type"], "message")
        self.assertEqual(teacher_saw["data"]["body"], "hello from teacher")

    def test_reaction_and_delete_events_reach_the_other_socket(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "react to me")
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)
        teacher_account = teacher_p.teacher_profile.user

        async def run():
            learner_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(learner_ws, conv.id, learner_account, "learner", learner_profile_id)
            await learner_ws.connect()
            await learner_ws.receive_json_from()  # history

            teacher_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(teacher_ws, conv.id, teacher_account, "teacher")
            await teacher_ws.connect()
            await teacher_ws.receive_json_from()  # history
            await learner_ws.receive_json_from()  # presence: teacher joined

            # Reaction + delete are REST mutations in production; here we
            # invoke the same service calls a view would, from a sync
            # thread, exactly like ReactToMessageView/DeleteMessageView do.
            def react_and_delete():
                action, summary = services.toggle_reaction(msg, teacher_p, "👍")
                services.realtime.push_conversation_event(conv.id, "chat.reaction", {
                    "message_id": str(msg.id), "reactions": summary,
                    "actor": teacher_p.identity_key(), "emoji": "👍", "action": action,
                })
                services.soft_delete_message(msg, participant=teacher_p)
                services.realtime.push_conversation_event(
                    conv.id, "chat.message_deleted", {"id": str(msg.id)},
                )

            await database_sync_to_async(react_and_delete)()

            reaction_evt = await learner_ws.receive_json_from()
            deleted_evt = await learner_ws.receive_json_from()

            await learner_ws.disconnect()
            await teacher_ws.disconnect()
            return reaction_evt, deleted_evt

        reaction_evt, deleted_evt = async_to_sync(run)()
        self.assertEqual(reaction_evt["type"], "reaction")
        self.assertEqual(reaction_evt["data"]["emoji"], "👍")
        self.assertEqual(deleted_evt["type"], "message_deleted")
        self.assertEqual(deleted_evt["data"]["id"], str(msg.id))

    def test_read_receipt_reaches_the_sender_but_not_an_echo_to_self(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)
        teacher_account = teacher_p.teacher_profile.user

        async def run():
            learner_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(learner_ws, conv.id, learner_account, "learner", learner_profile_id)
            await learner_ws.connect()
            await learner_ws.receive_json_from()

            teacher_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(teacher_ws, conv.id, teacher_account, "teacher")
            await teacher_ws.connect()
            await teacher_ws.receive_json_from()
            await learner_ws.receive_json_from()  # presence join

            # Teacher marks the thread read; the learner (the "sender" whose
            # messages are now known-read) should hear about it...
            await teacher_ws.send_json_to({"type": "read"})
            learner_read_evt = await learner_ws.receive_json_from()

            # ...but the teacher's OWN socket must NOT get an echo of their
            # own read receipt (chat_read()'s self-check).
            await teacher_ws.send_json_to({"type": "typing"})
            await learner_ws.receive_json_from()  # the corresponding typing event
            has_extra = await teacher_ws.receive_nothing(timeout=0.2)

            await learner_ws.disconnect()
            await teacher_ws.disconnect()
            return learner_read_evt, has_extra

        learner_read_evt, nothing_pending = async_to_sync(run)()
        self.assertEqual(learner_read_evt["type"], "read")
        self.assertEqual(learner_read_evt["data"]["identity"], teacher_p.identity_key())
        self.assertTrue(nothing_pending, "teacher's socket should not receive its own read echo")

    def test_rest_originated_attachment_reaches_an_open_socket_live(self):
        """The point of moving the group_send into
        services._finalize_new_message(): a message that never touched the
        WS consumer to be CREATED (an attachment upload is a REST call)
        still gets pushed to a conversation's live group."""
        conv, learner_p, teacher_p = make_direct_conversation()
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)

        async def run():
            learner_ws = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
            _scope_for(learner_ws, conv.id, learner_account, "learner", learner_profile_id)
            await learner_ws.connect()
            await learner_ws.receive_json_from()  # history

            def upload():
                f = SimpleUploadedFile("photo.png", b"data", content_type="image/png")
                return services.post_attachment_checked(conv, teacher_p, f, caption="look!")

            msg, error = await database_sync_to_async(upload)()
            assert error is None

            evt = await learner_ws.receive_json_from()
            await learner_ws.disconnect()
            return evt, msg

        evt, msg = async_to_sync(run)()
        self.assertEqual(evt["type"], "message")
        self.assertEqual(evt["data"]["id"], str(msg.id))
        self.assertEqual(evt["data"]["attachment"]["kind"], "IMAGE")
        self.assertEqual(evt["data"]["body"], "look!")
