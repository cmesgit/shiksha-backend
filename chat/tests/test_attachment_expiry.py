# Temporary file sharing (Phase 2): MessageAttachment.expires_at + the
# chat.tasks.expire_old_attachments sweep that soft-deletes the message
# behind an expired attachment.
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from chat.models import Message, MessageAttachment
from chat.tasks import expire_old_attachments

from .factories import make_direct_conversation


class ExpireOldAttachmentsTest(TestCase):
    def _make_attachment(self, conv, sender_participant, expires_at):
        msg = Message.objects.create(
            conversation=conv, sender=sender_participant,
            body="", message_type=Message.TYPE_FILE,
        )
        attachment = MessageAttachment.objects.create(
            conversation=conv, message=msg, uploaded_by=sender_participant,
            file="chat_attachments/test/dummy.pdf", kind=MessageAttachment.KIND_PDF,
            original_name="dummy.pdf", content_type="application/pdf", size_bytes=100,
            expires_at=expires_at,
        )
        return msg, attachment

    def test_expired_attachment_soft_deletes_its_message(self):
        conv, learner_p, _teacher_p = make_direct_conversation()
        msg, attachment = self._make_attachment(
            conv, learner_p, expires_at=timezone.now() - timedelta(days=1),
        )

        result = expire_old_attachments()
        self.assertEqual(result["expired"], 1)

        msg.refresh_from_db()
        self.assertIsNotNone(msg.deleted_at)
        self.assertIsNone(msg.deleted_by)
        self.assertEqual(msg.deleted_reason, "expired")

    def test_not_yet_expired_attachment_is_left_alone(self):
        conv, learner_p, _teacher_p = make_direct_conversation()
        msg, attachment = self._make_attachment(
            conv, learner_p, expires_at=timezone.now() + timedelta(days=5),
        )

        result = expire_old_attachments()
        self.assertEqual(result["expired"], 0)

        msg.refresh_from_db()
        self.assertIsNone(msg.deleted_at)

    def test_already_deleted_message_is_not_reprocessed(self):
        conv, learner_p, _teacher_p = make_direct_conversation()
        msg, attachment = self._make_attachment(
            conv, learner_p, expires_at=timezone.now() - timedelta(days=1),
        )
        msg.deleted_at = timezone.now()
        msg.deleted_reason = "Removed by a moderator"
        msg.save(update_fields=["deleted_at", "deleted_reason"])

        result = expire_old_attachments()
        self.assertEqual(result["expired"], 0)

        msg.refresh_from_db()
        # Untouched by the sweep — still carries the original reason.
        self.assertEqual(msg.deleted_reason, "Removed by a moderator")
