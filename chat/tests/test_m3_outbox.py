# M3 §11 — the transactional outbox: same-transaction write, drain_once()
# fan-out via notifications.services.notify() with correct
# audience_identity, and bounded retries.
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts.models import LearnerProfile
from chat import outbox_handlers, services
from chat.models import Conversation, Message, OutboxEvent, Participant
from notifications.models import Notification

from .factories import make_direct_conversation, make_learner, make_user


class OutboxTransactionalityTest(TestCase):
    """Proves the SAME-transaction guarantee by breaking it on purpose:
    if the OutboxEvent write fails, the Message it belongs to must not
    persist either. A test that only checks both rows exist after a
    successful call wouldn't actually prove atomicity — this does."""

    def test_message_insert_rolls_back_if_outbox_write_fails(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        with mock.patch(
            "chat.services.OutboxEvent.objects.create", side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                services.post_message(conv, teacher_p, "should not persist")

        self.assertFalse(
            Message.objects.filter(conversation=conv, body="should not persist").exists(),
        )

    def test_outbox_event_exists_after_a_normal_send(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "hello")
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type=OutboxEvent.EVENT_MESSAGE_CREATED,
                payload__message_id=str(msg.id),
                payload__conversation_id=str(conv.id),
            ).exists(),
        )
        # And it starts life unprocessed, waiting for the relay.
        event = OutboxEvent.objects.get(payload__message_id=str(msg.id))
        self.assertIsNone(event.processed_at)
        self.assertEqual(event.attempts, 0)


class DrainOnceNotifiesRecipientsTest(TestCase):

    def test_drain_notifies_the_other_participant_not_the_sender(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "hi from teacher")

        counts = outbox_handlers.drain_once()
        self.assertEqual(counts["processed"], 1)
        self.assertEqual(counts["failed"], 0)

        # Learner (recipient) got a Notification; teacher (sender) did not.
        self.assertTrue(
            Notification.objects.filter(
                recipient=learner_p.learner_profile.account, verb="chat.message",
            ).exists(),
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient=teacher_p.teacher_profile.user, verb="chat.message",
            ).exists(),
        )

    def test_notification_carries_the_correct_audience_identity(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "hi")
        outbox_handlers.drain_once()

        n = Notification.objects.get(
            recipient=learner_p.learner_profile.account, verb="chat.message",
        )
        self.assertEqual(n.audience_identity, learner_p.identity_key())
        self.assertEqual(n.link_url, f"/chat/{conv.id}")

    def test_offline_recipient_gets_a_persisted_notification(self):
        """The whole point of the outbox (gap G5): unlike the M0
        inbox_delta (which only reaches an OPEN socket), a Notification
        ROW exists for an offline recipient to see whenever they next
        open the app — nothing about drain_once() depends on a live
        connection."""
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "are you there?")
        outbox_handlers.drain_once()

        self.assertTrue(
            Notification.objects.filter(
                recipient=learner_p.learner_profile.account,
                verb="chat.message",
                is_read=False,
            ).exists(),
        )

    def test_sibling_profile_does_not_get_the_notification(self):
        """A ROOM message to one sibling's participant must not surface on
        the OTHER sibling's dashboard, even though they share the same
        underlying account/User row — the exact audience_identity
        precision M2 exists for, now exercised through the M3 relay."""
        account = make_user()
        child_a = make_learner(account=account, display_name="Child A",
                                relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)
        child_b = make_learner(account=account, display_name="Child B",
                                relationship=LearnerProfile.RELATIONSHIP_DEPENDENT)
        _, _, teacher_p = make_direct_conversation()

        room = Conversation.objects.create(kind=Conversation.KIND_ROOM, context_type="course", context_id="c1")
        a_participant = services._attach_participant(room, Participant.KIND_LEARNER, child_a)
        services._attach_participant(room, Participant.KIND_LEARNER, child_b)
        services._attach_participant(room, Participant.KIND_TEACHER, teacher_p.teacher_profile)

        services.post_message(room, teacher_p, "note for the class")
        outbox_handlers.drain_once()

        from django.db.models import Q
        a_key, b_key = f"L:{child_a.id}", f"L:{child_b.id}"
        a_dashboard = Notification.objects.filter(recipient=account, verb="chat.message").filter(
            Q(audience_identity="") | Q(audience_identity=a_key)
        )
        b_dashboard = Notification.objects.filter(recipient=account, verb="chat.message").filter(
            Q(audience_identity="") | Q(audience_identity=b_key)
        )
        self.assertEqual(a_dashboard.count(), 1)
        self.assertEqual(b_dashboard.count(), 1)
        # And, critically, each sibling's row is scoped to THEM, not "either":
        self.assertEqual(a_dashboard.first().audience_identity, a_key)
        self.assertEqual(b_dashboard.first().audience_identity, b_key)


class DrainOnceIdempotencyAndRetriesTest(TestCase):

    def test_processed_event_is_not_picked_up_again(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "hi")

        first = outbox_handlers.drain_once()
        second = outbox_handlers.drain_once()

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 0)  # nothing left to even claim

    def test_failure_increments_attempts_and_is_retried(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "hi")
        event = OutboxEvent.objects.get(event_type=OutboxEvent.EVENT_MESSAGE_CREATED)

        with mock.patch(
            "chat.outbox_handlers._handle_message_created", side_effect=RuntimeError("transient"),
        ):
            counts = outbox_handlers.drain_once()
        self.assertEqual(counts["failed"], 1)

        event.refresh_from_db()
        self.assertEqual(event.attempts, 1)
        self.assertIsNone(event.processed_at)
        self.assertIn("transient", event.last_error)

        # Next drain (failure gone) picks it right back up and succeeds.
        counts = outbox_handlers.drain_once()
        self.assertEqual(counts["processed"], 1)
        event.refresh_from_db()
        self.assertIsNotNone(event.processed_at)

    def test_retries_are_bounded_by_max_attempts(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, teacher_p, "hi")
        event = OutboxEvent.objects.get(event_type=OutboxEvent.EVENT_MESSAGE_CREATED)
        event.attempts = OutboxEvent.MAX_ATTEMPTS
        event.save(update_fields=["attempts"])

        with mock.patch(
            "chat.outbox_handlers._handle_message_created", side_effect=RuntimeError("still broken"),
        ):
            counts = outbox_handlers.drain_once()

        # The row is at the ceiling already, so drain_once() must not even
        # select it — the bound is enforced by the query, not a runtime check.
        self.assertEqual(counts, {"processed": 0, "failed": 0, "skipped": 0})
        event.refresh_from_db()
        self.assertEqual(event.attempts, OutboxEvent.MAX_ATTEMPTS)

    def test_missing_message_is_skipped_not_retried_forever(self):
        """If the Message a queued event points at is gone by drain time,
        that's a permanent no-op, not a transient failure — the row
        should be marked processed, not left to retry forever."""
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "will be deleted")
        msg.delete()

        counts = outbox_handlers.drain_once()
        self.assertEqual(counts["processed"], 1)
        self.assertEqual(counts["failed"], 0)

    def test_unknown_event_type_does_not_crash_the_drain(self):
        OutboxEvent.objects.create(event_type="chat.something_unrecognized", payload={})
        counts = outbox_handlers.drain_once()
        self.assertEqual(counts["processed"], 1)  # logged + marked done, not raised
