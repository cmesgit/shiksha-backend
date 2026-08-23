# Regression tests for the 2026-08-22 chat audit's remaining 3 CRITICAL +
# 7 backend HIGH fixes (the 2 frontend-only HIGH items — mobile layout,
# a11y/modal focus — have no backend surface to test here; see the browser
# verification pass instead). Each test proves the SPECIFIC failure mode
# the audit named, not just "the code runs" — several were written against
# the old code first to confirm they actually fail there.
import uuid
from unittest.mock import patch
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from chat import services, policy
from chat.consumers import ChatConsumer
from chat.models import (
    Conversation, Participant, Message, MessageAttachment, CommPreference,
)
from chat import views as chat_views
from chat import tasks as chat_tasks
from chat.outbox_handlers import _handle_message_created
from config.media_security import _check_chat_attachment

from .factories import (
    make_user, make_learner, make_teacher, make_course, make_subject,
    assign_teacher_to_subject, make_active_subscription,
    make_expired_subscription, enrolled_learner_and_teacher,
    make_direct_conversation,
)


def _auth_for(kind, obj):
    if kind == Participant.KIND_LEARNER:
        return {"context": "learner", "active_profile": str(obj.id)}
    return {"context": "teacher"}


def _call(view_cls, method, path, user, auth, data=None, **kwargs):
    factory = APIRequestFactory()
    fn = getattr(factory, method)
    request = fn(path, data or {}, format="json")
    force_authenticate(request, user=user, token=auth)
    return view_cls.as_view()(request, **kwargs)


# ===========================================================================
# Fix 1 (CRITICAL) — retrying a failed send gets a real broadcast, not silence
# ===========================================================================

class RetryDedupeBroadcastsTest(TestCase):

    def test_resending_the_same_client_id_still_broadcasts(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        cid = str(uuid.uuid4())

        with patch("chat.services.realtime.push_conversation_event") as push:
            first = services.post_message(conv, learner_p, "hi", client_id=cid)
            self.assertEqual(push.call_count, 1)  # from _finalize_new_message

            second = services.post_message(conv, learner_p, "hi", client_id=cid)
            self.assertEqual(second.id, first.id, "dedupe must still return the same row")
            # The whole point of this fix: a resend must ALSO broadcast, or
            # the sender's client times out and shows "failed" forever even
            # though the message was delivered the first time.
            self.assertEqual(
                push.call_count, 2,
                "resending an already-sent client_id produced no broadcast — "
                "the exact bug: the sender's retry gets permanent silence",
            )
            second_call_data = push.call_args[0][2]
            self.assertEqual(second_call_data["id"], str(first.id))

    def test_resending_does_not_create_a_second_outbox_event_or_double_notify(self):
        from chat.models import OutboxEvent
        conv, learner_p, teacher_p = make_direct_conversation()
        cid = str(uuid.uuid4())

        services.post_message(conv, learner_p, "hi", client_id=cid)
        count_after_first = OutboxEvent.objects.count()
        services.post_message(conv, learner_p, "hi", client_id=cid)
        self.assertEqual(
            OutboxEvent.objects.count(), count_after_first,
            "a resend must not create a second OutboxEvent — that would "
            "double-notify every other participant",
        )


# ===========================================================================
# Fix 4 (HIGH) — mute suppresses notifications only (badge/inbox stay live)
# ===========================================================================

class MuteSuppressesNotificationsOnlyTest(TestCase):

    def test_muted_participant_is_not_notified(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        teacher_p.muted_until = timezone.now() + timedelta(hours=8)
        teacher_p.save(update_fields=["muted_until"])

        msg = services.post_message(conv, learner_p, "are you there?")

        with patch("notifications.services.notify") as notify:
            _handle_message_created({
                "conversation_id": str(conv.id), "message_id": str(msg.id),
            })
            self.assertEqual(
                notify.call_count, 0,
                "a muted participant was still notified",
            )

    def test_unmuted_participant_still_gets_notified(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "are you there?")

        with patch("notifications.services.notify") as notify:
            _handle_message_created({
                "conversation_id": str(conv.id), "message_id": str(msg.id),
            })
            self.assertEqual(notify.call_count, 1)

    def test_mute_does_not_suppress_the_unread_counter(self):
        """The resolved scope: mute silences notifications, it does NOT
        hide the thread — unread/inbox_delta (_fanout_new_message) must
        stay untouched by this fix."""
        conv, learner_p, teacher_p = make_direct_conversation()
        teacher_p.muted_until = timezone.now() + timedelta(hours=8)
        teacher_p.save(update_fields=["muted_until"])

        services.post_message(conv, learner_p, "are you there?")
        unread = services._unread_from_db(conv, teacher_p)
        self.assertEqual(unread, 1, "muting must not suppress the unread counter")

    def test_expired_mute_no_longer_suppresses(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        teacher_p.muted_until = timezone.now() - timedelta(minutes=1)  # already expired
        teacher_p.save(update_fields=["muted_until"])
        msg = services.post_message(conv, learner_p, "hi")

        with patch("notifications.services.notify") as notify:
            _handle_message_created({
                "conversation_id": str(conv.id), "message_id": str(msg.id),
            })
            self.assertEqual(notify.call_count, 1)


# ===========================================================================
# Fix 5 (HIGH) — skill-track course rooms are reachable
# ===========================================================================

class SkillTrackCourseRoomAccessTest(TestCase):

    def test_learner_with_only_a_skill_enrollment_can_join(self):
        from skills.course_models import SkillCourse, SkillCourseEnrollment

        teacher = make_teacher()
        skill_course = SkillCourse.objects.create(
            teacher_profile=teacher, title="Guitar Basics",
        )
        learner = make_learner()
        SkillCourseEnrollment.objects.create(
            learner_profile=learner, course=skill_course,
            status=SkillCourseEnrollment.STATUS_ACTIVE,
        )
        self.assertTrue(
            services.learner_in_course(learner, skill_course.id),
            "a learner actively enrolled in a SkillCourse (no academy "
            "Course/Enrollment at all) must be able to join its room",
        )

    def test_learner_with_no_enrollment_anywhere_cannot_join(self):
        from skills.course_models import SkillCourse
        teacher = make_teacher()
        skill_course = SkillCourse.objects.create(
            teacher_profile=teacher, title="Guitar Basics",
        )
        learner = make_learner()
        self.assertFalse(services.learner_in_course(learner, skill_course.id))

    def test_owning_teacher_can_join_their_skill_course_room(self):
        from skills.course_models import SkillCourse
        teacher = make_teacher()
        skill_course = SkillCourse.objects.create(
            teacher_profile=teacher, title="Guitar Basics",
        )
        self.assertTrue(services.teacher_in_course(teacher, skill_course.id))

    def test_academy_course_access_still_works_unchanged(self):
        """The SkillCourse fallback must not break the existing academy path."""
        learner, teacher, course = enrolled_learner_and_teacher()
        self.assertTrue(services.learner_in_course(learner, course.id))
        self.assertTrue(services.teacher_in_course(teacher, course.id))

    def test_academy_course_with_lapsed_subscription_still_denied(self):
        """A real Course lookup that resolves but fails the subscription
        check must return False directly — not fall through and get a
        false positive by accidentally matching a SkillCourse row."""
        course = make_course()
        subject = make_subject(course=course)
        teacher = make_teacher()
        assign_teacher_to_subject(subject, teacher)
        learner = make_learner()
        make_expired_subscription(learner, course)
        self.assertFalse(services.learner_in_course(learner, course.id))


# ===========================================================================
# Fix 6 (HIGH) — attachment media guard is profile-scoped, not account-scoped
# ===========================================================================

class AttachmentMediaGuardProfileScopedTest(TestCase):

    def _request_as(self, kind, obj, user):
        factory = APIRequestFactory()
        request = factory.get("/media/secure/whatever")
        request.user = user
        force_authenticate(request, user=user, token=_auth_for(kind, obj))
        # force_authenticate sets request.auth via DRF's request wrapper,
        # which only takes effect once DRF's Request has parsed auth —
        # media_security's checker reads request.auth directly, so mirror
        # what active_identity_from_request() expects without needing a
        # full view dispatch.
        request.auth = _auth_for(kind, obj)
        return request

    def test_sibling_profile_cannot_download_a_conversation_they_are_not_in(self):
        account = make_user()
        profile_a = make_learner(account=account, display_name="Child A")
        profile_b = make_learner(account=account, display_name="Child B")
        teacher = make_teacher()
        conv = services.ensure_direct(
            Participant.KIND_LEARNER, profile_a, Participant.KIND_TEACHER, teacher,
        )
        attachment_path = f"chat_attachments/{conv.id}/some-file.png"

        request_b = self._request_as(Participant.KIND_LEARNER, profile_b, account)
        self.assertFalse(
            _check_chat_attachment(request_b, attachment_path),
            "a sibling profile on the same account could download an "
            "attachment from a conversation only their sibling is in",
        )

        request_a = self._request_as(Participant.KIND_LEARNER, profile_a, account)
        self.assertTrue(
            _check_chat_attachment(request_a, attachment_path),
            "the actual participant profile must still be able to download it",
        )

    def test_staff_can_still_access_regardless(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        staff_user = make_user()
        staff_user.is_staff = True
        staff_user.save(update_fields=["is_staff"])
        factory = APIRequestFactory()
        request = factory.get("/media/secure/whatever")
        request.user = staff_user  # is_staff short-circuits before auth is read
        self.assertTrue(
            _check_chat_attachment(request, f"chat_attachments/{conv.id}/f.png"),
        )


# ===========================================================================
# Fix 7 (HIGH) — react/delete re-check live course membership
# ===========================================================================

class ReactDeleteMembershipRecheckTest(TestCase):

    def _course_room_with_lapsed_learner(self):
        course = make_course()
        subject = make_subject(course=course)
        teacher = make_teacher()
        assign_teacher_to_subject(subject, teacher)
        learner = make_learner()
        make_active_subscription(learner, course)
        conv = services.ensure_course_room(course.id, title=course.title)
        teacher_p = services._attach_participant(conv, Participant.KIND_TEACHER, teacher)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner)
        msg = services.post_message(conv, teacher_p, "welcome to class")
        # Subscription lapses AFTER the Participant row already exists —
        # the exact "stale row, revoked access" scenario this fix closes.
        from enrollments.models import Subscription
        Subscription.objects.filter(learner_profile=learner).update(
            status=Subscription.STATUS_EXPIRED,
            expires_at=timezone.now() - timedelta(days=1),
        )
        return learner, msg

    def test_react_denied_once_course_access_lapsed(self):
        learner, msg = self._course_room_with_lapsed_learner()
        resp = _call(
            chat_views.ReactToMessageView, "post",
            f"/chat/messages/{msg.id}/react/",
            learner.account, _auth_for(Participant.KIND_LEARNER, learner),
            data={"emoji": "👍"}, message_id=msg.id,
        )
        self.assertEqual(
            resp.status_code, 403,
            "a learner whose course access lapsed could still react in that room",
        )

    def test_delete_denied_once_course_access_lapsed(self):
        learner, msg = self._course_room_with_lapsed_learner()
        # Give the (now-lapsed) learner their own message to try to delete.
        learner_p = services.participant_for(msg.conversation, Participant.KIND_LEARNER, learner)
        own_msg = services.post_message(msg.conversation, learner_p, "a message of mine")
        resp = _call(
            chat_views.DeleteMessageView, "post",
            f"/chat/messages/{own_msg.id}/delete/",
            learner.account, _auth_for(Participant.KIND_LEARNER, learner),
            message_id=own_msg.id,
        )
        self.assertEqual(resp.status_code, 403)

    def test_react_still_works_for_a_current_member(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "hi")
        resp = _call(
            chat_views.ReactToMessageView, "post",
            f"/chat/messages/{msg.id}/react/",
            learner_p.learner_profile.account,
            _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile),
            data={"emoji": "👍"}, message_id=msg.id,
        )
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# Fix 8 (HIGH) — can_start_dm refuses a blocked pair (the DM-shell gap)
# ===========================================================================

class CanStartDmBlockCheckTest(TestCase):

    def test_blocked_pair_cannot_start_a_dm(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        services.create_block(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_unblocked_pair_with_a_shared_course_can_still_start_a_dm(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertTrue(allowed, reason)


# ===========================================================================
# Fix 9 (HIGH) — CommPreference is honoured over the WebSocket too
# ===========================================================================

class CommPreferenceOverWebSocketTest(TransactionTestCase):

    def test_presence_hidden_when_show_online_status_is_off(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        CommPreference.objects.update_or_create(
            identity_key=teacher_p.identity_key(),
            defaults={"show_online_status": False},
        )
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)
        teacher_account = teacher_p.teacher_profile.user

        async def run():
            learner_comm = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            learner_comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            learner_comm.scope["user"] = learner_account
            learner_comm.scope["context"] = "learner"
            learner_comm.scope["active_profile_id"] = learner_profile_id
            learner_comm.scope["identity"] = None
            connected, _ = await learner_comm.connect()
            assert connected
            await learner_comm.receive_json_from()  # history frame

            teacher_comm = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            teacher_comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            teacher_comm.scope["user"] = teacher_account
            teacher_comm.scope["context"] = "teacher"
            teacher_comm.scope["active_profile_id"] = None
            teacher_comm.scope["identity"] = None
            t_connected, _ = await teacher_comm.connect()
            assert t_connected
            await teacher_comm.receive_json_from()  # teacher's own history frame

            # The learner should NOT receive a presence frame for the
            # teacher's connect, since the teacher disabled show_online_status.
            got_presence = await learner_comm.receive_nothing(timeout=0.5)
            await teacher_comm.disconnect()
            await learner_comm.disconnect()
            return got_presence

        received_nothing = async_to_sync(run)()
        self.assertTrue(
            received_nothing,
            "the learner received a presence frame despite the teacher's "
            "show_online_status preference being off",
        )

    def test_presence_still_shown_when_preference_is_on(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        # Explicit default — show_online_status defaults True.
        CommPreference.objects.update_or_create(
            identity_key=teacher_p.identity_key(),
            defaults={"show_online_status": True},
        )
        learner_account = learner_p.learner_profile.account
        learner_profile_id = str(learner_p.learner_profile_id)
        teacher_account = teacher_p.teacher_profile.user

        async def run():
            learner_comm = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            learner_comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            learner_comm.scope["user"] = learner_account
            learner_comm.scope["context"] = "learner"
            learner_comm.scope["active_profile_id"] = learner_profile_id
            learner_comm.scope["identity"] = None
            connected, _ = await learner_comm.connect()
            assert connected
            await learner_comm.receive_json_from()

            teacher_comm = WebsocketCommunicator(
                ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/",
            )
            teacher_comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
            teacher_comm.scope["user"] = teacher_account
            teacher_comm.scope["context"] = "teacher"
            teacher_comm.scope["active_profile_id"] = None
            teacher_comm.scope["identity"] = None
            t_connected, _ = await teacher_comm.connect()
            assert t_connected
            await teacher_comm.receive_json_from()

            frame = await learner_comm.receive_json_from(timeout=2)
            await teacher_comm.disconnect()
            await learner_comm.disconnect()
            return frame

        frame = async_to_sync(run)()
        self.assertEqual(frame["type"], "presence")


# ===========================================================================
# Fix 10 (HIGH) — expired/removed attachments are actually deleted
# ===========================================================================

class AttachmentExpiryActuallyDeletesFileTest(TestCase):

    def _conversation_with_attachment(self, expires_at):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = Message.objects.create(
            conversation=conv, sender=learner_p, body="", message_type=Message.TYPE_TEXT,
        )
        stored = default_storage.save(
            f"chat_attachments/{conv.id}/test.png", ContentFile(b"fake-bytes"),
        )
        attachment = MessageAttachment.objects.create(
            conversation=conv, message=msg, file=stored,
            kind=MessageAttachment.KIND_IMAGE, expires_at=expires_at,
        )
        return conv, msg, attachment, stored

    def test_expiry_sweep_deletes_the_file_from_storage(self):
        conv, msg, attachment, stored_name = self._conversation_with_attachment(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertTrue(default_storage.exists(stored_name))

        chat_tasks.expire_old_attachments()

        self.assertFalse(
            default_storage.exists(stored_name),
            "expire_old_attachments() left the file on disk after expiring the message",
        )
        msg.refresh_from_db()
        self.assertIsNotNone(msg.deleted_at)

    def test_self_delete_also_purges_the_file(self):
        conv, msg, attachment, stored_name = self._conversation_with_attachment(
            expires_at=timezone.now() + timedelta(days=5),  # not expired
        )
        learner_p = msg.sender
        services.soft_delete_message(msg, participant=learner_p)
        self.assertFalse(default_storage.exists(stored_name))

    def test_deleted_attachment_is_denied_even_to_a_current_participant(self):
        conv, msg, attachment, stored_name = self._conversation_with_attachment(
            expires_at=timezone.now() + timedelta(days=5),
        )
        learner_p = msg.sender
        services.soft_delete_message(msg, participant=learner_p)

        factory = APIRequestFactory()
        request = factory.get("/media/secure/whatever")
        account = learner_p.learner_profile.account
        request.user = account
        auth = _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile)
        force_authenticate(request, user=account, token=auth)
        request.auth = auth

        self.assertFalse(
            _check_chat_attachment(request, stored_name),
            "a removed attachment stayed downloadable to a non-staff participant",
        )
