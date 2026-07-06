# M0 regression (Phase 3 §13/§16/§32) — re-verified as part of M3 per this
# stage's "full M0/M1/M2 regression" requirement. No suite for this existed
# in the codebase before this stage; written fresh here, against a REAL
# local redis-server (not a mock) for the counter/rate-limit behaviour,
# per the "verify against real dependencies" instruction for this stage.
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase

from chat import redis_utils, services
from chat.models import Conversation, Message, Participant

from .factories import make_direct_conversation as _direct_conversation


class UnreadCounterTest(TestCase):
    """redis_utils' unread counter, exercised through the real send path
    (post_message_checked -> _fanout_new_message), against a real Redis —
    fresh identity keys every test (a new UUID per make_learner/make_teacher
    call), so no cross-test cleanup is needed."""

    def test_sending_increments_recipients_unread_not_senders(self):
        conv, learner_p, teacher_p = _direct_conversation()

        msg, err = services.post_message_checked(conv, teacher_p, "hello there")
        self.assertIsNone(err)
        self.assertIsNotNone(msg)

        # Recipient: Redis was actually incremented — a real cache HIT,
        # checked directly against the raw hash field.
        raw = redis_utils.get_redis().hget(
            f"unread:{learner_p.identity_key()}", str(conv.id),
        )
        self.assertEqual(raw, "1")

        # Sender: their own counter is never touched by their own send —
        # _fanout_new_message() explicitly excludes the sender — so the
        # field simply doesn't exist yet (there's no reason to count your
        # own message as unread to yourself).
        sender_has_entry = redis_utils.get_redis().hexists(
            f"unread:{teacher_p.identity_key()}", str(conv.id),
        )
        self.assertFalse(sender_has_entry)

        # Queried anyway (e.g. opening your own sent thread), the DB-truth
        # rebuild correctly computes 0.
        sender_count = redis_utils.get_unread_count(
            teacher_p.identity_key(), conv.id,
            rebuild_fn=lambda: services._unread_from_db(conv, teacher_p),
        )
        self.assertEqual(sender_count, 0)

    def test_mark_read_clears_the_counter(self):
        conv, learner_p, teacher_p = _direct_conversation()
        services.post_message_checked(conv, teacher_p, "hello")

        # Mirror the real mark-read flow (chat/consumers.py._mark_read):
        # both the DB last_read_at AND the Redis counter move together.
        from django.utils import timezone
        learner_p.last_read_at = timezone.now()
        learner_p.save(update_fields=["last_read_at"])
        redis_utils.clear_unread(learner_p.identity_key(), conv.id)

        count = redis_utils.get_unread_count(
            learner_p.identity_key(), conv.id,
            rebuild_fn=lambda: services._unread_from_db(conv, learner_p),
        )
        self.assertEqual(count, 0)

    def test_cache_miss_rebuilds_from_db_truth(self):
        conv, learner_p, teacher_p = _direct_conversation()
        services.post_message_checked(conv, teacher_p, "one")
        services.post_message_checked(conv, teacher_p, "two")

        # Force a real cache miss by deleting straight from Redis, then
        # confirm get_unread_count() falls back to the DB and gets the
        # right answer rather than silently returning 0/stale.
        redis_utils.get_redis().delete(f"unread:{learner_p.identity_key()}")

        count = redis_utils.get_unread_count(
            learner_p.identity_key(), conv.id,
            rebuild_fn=lambda: services._unread_from_db(conv, learner_p),
        )
        self.assertEqual(count, 2)


class RateLimitTest(TestCase):
    """Fixed-window rate limiting against real Redis. Each test uses a
    fresh random key so tests never interfere with each other even though
    Redis state isn't reset between tests the way the SQL test DB is."""

    def test_allows_up_to_the_burst_limit_then_blocks(self):
        key = f"m0-ratelimit-{uuid.uuid4().hex}"
        for i in range(redis_utils.RATE_BURST_LIMIT):
            self.assertIsNone(
                redis_utils.check_message_rate_limit(key), f"call #{i + 1} should be allowed",
            )
        self.assertIsNotNone(
            redis_utils.check_message_rate_limit(key), "call past the burst limit should be blocked",
        )

    def test_connect_rate_limit_allows_then_blocks(self):
        key = f"m0-connect-{uuid.uuid4().hex}"
        for _ in range(redis_utils.CONNECT_LIMIT):
            self.assertTrue(redis_utils.check_connect_rate_limit(key))
        self.assertFalse(redis_utils.check_connect_rate_limit(key))


class MessageDedupeTest(TestCase):
    """The M0-hardening unique constraint (conversation, sender, client_id)
    where client_id != "", and post_message()'s idempotent check-before-
    insert in front of it."""

    def test_duplicate_client_id_same_sender_violates_db_constraint(self):
        conv, learner_p, _ = _direct_conversation()
        Message.objects.create(conversation=conv, sender=learner_p, body="hi", client_id="dup-1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Message.objects.create(
                    conversation=conv, sender=learner_p, body="hi again", client_id="dup-1",
                )

    def test_blank_client_id_is_exempt_from_the_constraint(self):
        """Server-authored/legacy rows with no client_id must NOT collide —
        the constraint is scoped to non-blank client_id."""
        conv, learner_p, _ = _direct_conversation()
        Message.objects.create(conversation=conv, sender=learner_p, body="a", client_id="")
        Message.objects.create(conversation=conv, sender=learner_p, body="b", client_id="")
        self.assertEqual(
            Message.objects.filter(conversation=conv, client_id="").count(), 2,
        )

    def test_post_message_is_idempotent_on_client_id(self):
        conv, learner_p, _ = _direct_conversation()
        msg1 = services.post_message(conv, learner_p, "hi", client_id="retry-key")
        msg2 = services.post_message(conv, learner_p, "hi", client_id="retry-key")
        self.assertEqual(msg1.id, msg2.id)
        self.assertEqual(
            Message.objects.filter(conversation=conv, client_id="retry-key").count(), 1,
        )

    def test_same_client_id_different_senders_does_not_collide(self):
        conv, learner_p, teacher_p = _direct_conversation()
        m1 = services.post_message(conv, learner_p, "from learner", client_id="shared-id")
        m2 = services.post_message(conv, teacher_p, "from teacher", client_id="shared-id")
        self.assertNotEqual(m1.id, m2.id)
