# Communication Center gap-analysis closure — Stage B (conversation
# management, replies, soft delete, reactions), Stage C (attachments,
# course-hub composition), Stage D (announcements, support tickets, admin
# console). Not part of the M0–M3 stages above; new coverage only — the
# other files in this directory already regression-test everything M0–M3
# shipped, and this file leaves all of that untouched.
import io
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import Identity
from chat import services
from chat.models import (
    Conversation, Participant, Message, MessageAttachment, MessageReaction,
    Report, ChatSuspension, CommPreference, SupportTicket,
)
from chat import views as chat_views

from .factories import (
    make_user, make_learner, make_teacher, make_course, make_subject,
    assign_teacher_to_subject, make_active_subscription,
    enrolled_learner_and_teacher, make_direct_conversation,
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
# Stage B — reply, soft delete, reactions, pin/archive/mute, report
# ===========================================================================

class ReplyAndSoftDeleteTest(TestCase):

    def test_reply_to_carries_a_preview_of_the_parent(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        parent = services.post_message(conv, learner_p, "What time is class?")
        reply, error = services.post_message_checked(
            conv, teacher_p, "3pm", reply_to_id=str(parent.id),
        )
        self.assertIsNone(error)
        data = services.serialize_message(reply)
        self.assertEqual(data["reply_to"]["id"], str(parent.id))
        self.assertEqual(data["reply_to"]["body_preview"], "What time is class?")

    def test_an_invalid_reply_to_id_is_silently_dropped_not_refused(self):
        conv, learner_p, _ = make_direct_conversation()
        msg, error = services.post_message_checked(
            conv, learner_p, "hello", reply_to_id=str(uuid.uuid4()),
        )
        self.assertIsNone(error)
        self.assertIsNotNone(msg)
        self.assertIsNone(msg.reply_to_id)

    def test_sender_can_delete_their_own_message(self):
        conv, learner_p, _ = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "oops typo")
        self.assertTrue(services.can_delete_message(learner_p, msg))
        services.soft_delete_message(msg, participant=learner_p)
        msg.refresh_from_db()
        data = services.serialize_message(msg)
        self.assertTrue(data["deleted"])
        self.assertEqual(data["body"], "")
        self.assertEqual(data["deleted_reason"], "")

    def test_recipient_cannot_delete_someone_elses_direct_message(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "hi")
        self.assertFalse(services.can_delete_message(teacher_p, msg))

    def test_teacher_can_moderate_a_room_message_learner_cannot(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_room(course.id)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner)
        services._attach_participant(conv, Participant.KIND_TEACHER, teacher)
        learner_p = services.participant_for(conv, Participant.KIND_LEARNER, learner)
        teacher_p = services.participant_for(conv, Participant.KIND_TEACHER, teacher)

        msg = services.post_message(conv, learner_p, "off topic message")
        self.assertTrue(services.can_delete_message(teacher_p, msg))

        # A second, different learner may NOT delete the first learner's message.
        learner2 = make_learner()
        make_active_subscription(learner2, course)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner2)
        learner2_p = services.participant_for(conv, Participant.KIND_LEARNER, learner2)
        self.assertFalse(services.can_delete_message(learner2_p, msg))

    def test_admin_removal_leaves_no_deleted_by_participant_but_sets_reason(self):
        conv, learner_p, _ = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "reported text")
        services.soft_delete_message(msg, participant=None, admin_reason="Removed by a moderator")
        msg.refresh_from_db()
        self.assertIsNone(msg.deleted_by_id)
        self.assertEqual(msg.deleted_reason, "Removed by a moderator")
        data = services.serialize_message(msg)
        self.assertTrue(data["deleted"])

    def test_delete_view_rejects_a_non_owner(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "mine")
        response = _call(
            chat_views.DeleteMessageView, "post", "/api/chat/messages/x/delete/",
            teacher_p.teacher_profile.user, _auth_for(Participant.KIND_TEACHER, teacher_p.teacher_profile),
            message_id=msg.id,
        )
        self.assertEqual(response.status_code, 403)


class ReactionTest(TestCase):

    def test_same_emoji_twice_toggles_off(self):
        conv, learner_p, _ = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "hi")
        action1, summary1 = services.toggle_reaction(msg, learner_p, "👍")
        self.assertEqual(action1, "added")
        self.assertEqual(summary1, [{"emoji": "👍", "count": 1, "identities": [learner_p.identity_key()]}])

        action2, summary2 = services.toggle_reaction(msg, learner_p, "👍")
        self.assertEqual(action2, "removed")
        self.assertEqual(summary2, [])

    def test_two_participants_can_react_with_the_same_emoji(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "hi")
        services.toggle_reaction(msg, learner_p, "❤️")
        _, summary = services.toggle_reaction(msg, teacher_p, "❤️")
        self.assertEqual(summary[0]["count"], 2)

    def test_view_rejects_an_emoji_outside_the_allowed_set(self):
        conv, learner_p, _ = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "hi")
        response = _call(
            chat_views.ReactToMessageView, "post", "/api/chat/messages/x/react/",
            learner_p.learner_profile.account, _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile),
            data={"emoji": "🤷‍♂️not-a-real-reaction"},
            message_id=msg.id,
        )
        self.assertEqual(response.status_code, 400)


class PinArchiveMuteTest(TestCase):

    def _auth_and_user(self, learner_p):
        return learner_p.learner_profile.account, _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile)

    def test_pin_toggles(self):
        conv, learner_p, _ = make_direct_conversation()
        user, auth = self._auth_and_user(learner_p)
        r1 = _call(chat_views.PinConversationView, "post", "/x", user, auth, conversation_id=conv.id)
        self.assertTrue(r1.data["pinned"])
        r2 = _call(chat_views.PinConversationView, "post", "/x", user, auth, conversation_id=conv.id)
        self.assertFalse(r2.data["pinned"])

    def test_archive_is_the_cc006_delete_action_and_is_reversible(self):
        conv, learner_p, _ = make_direct_conversation()
        user, auth = self._auth_and_user(learner_p)
        r1 = _call(chat_views.ArchiveConversationView, "post", "/x", user, auth, conversation_id=conv.id)
        self.assertTrue(r1.data["archived"])
        learner_p.refresh_from_db()
        self.assertIsNotNone(learner_p.archived_at)
        r2 = _call(chat_views.ArchiveConversationView, "post", "/x", user, auth,
                    data={"archived": False}, conversation_id=conv.id)
        self.assertFalse(r2.data["archived"])

    def test_mute_for_minutes_then_unmute(self):
        conv, learner_p, _ = make_direct_conversation()
        user, auth = self._auth_and_user(learner_p)
        r1 = _call(chat_views.MuteConversationView, "post", "/x", user, auth,
                    data={"minutes": 60}, conversation_id=conv.id)
        self.assertIsNotNone(r1.data["muted_until"])
        r2 = _call(chat_views.MuteConversationView, "post", "/x", user, auth,
                    data={"unmute": True}, conversation_id=conv.id)
        self.assertIsNone(r2.data["muted_until"])


class ReportTest(TestCase):

    def test_reporting_a_message_captures_the_senders_identity_as_target(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "some message")
        user, auth = learner_p.learner_profile.account, _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile)
        response = _call(
            chat_views.ReportView, "post", "/x", user, auth,
            data={"message_id": str(msg.id), "reason": Report.REASON_HARASSMENT, "detail": "not cool"},
            conversation_id=conv.id,
        )
        self.assertEqual(response.status_code, 201)
        report = Report.objects.get(id=response.data["id"])
        self.assertEqual(report.target_identity, teacher_p.identity_key())
        self.assertEqual(report.reporter_id, learner_p.id)


# ===========================================================================
# Stage D — chat-level suspension
# ===========================================================================

class SuspensionTest(TestCase):

    def test_suspended_identity_is_refused_regardless_of_content(self):
        conv, learner_p, _ = make_direct_conversation()
        services.suspend_identity(learner_p.identity_key(), "spamming", created_by=None)
        msg, error = services.post_message_checked(conv, learner_p, "a perfectly polite message")
        self.assertIsNone(msg)
        self.assertEqual(error["category"], "suspended")

    def test_expired_suspension_no_longer_blocks(self):
        conv, learner_p, _ = make_direct_conversation()
        ChatSuspension.objects.create(
            identity_key=learner_p.identity_key(),
            suspended_until=timezone.now() - timezone.timedelta(minutes=1),
        )
        msg, error = services.post_message_checked(conv, learner_p, "hello again")
        self.assertIsNone(error)
        self.assertIsNotNone(msg)

    def test_lift_suspension_removes_the_row(self):
        key = "L:" + str(uuid.uuid4())
        services.suspend_identity(key, "test", created_by=None)
        self.assertEqual(ChatSuspension.objects.filter(identity_key=key).count(), 1)
        services.lift_suspension(key)
        self.assertEqual(ChatSuspension.objects.filter(identity_key=key).count(), 0)


# ===========================================================================
# Stage C — attachments
# ===========================================================================

class AttachmentTest(TestCase):

    def test_a_supported_image_is_accepted_and_broadcastable(self):
        conv, learner_p, _ = make_direct_conversation()
        f = SimpleUploadedFile("photo.png", b"not-really-a-png-but-fine-for-this-test", content_type="image/png")
        msg, error = services.post_attachment_checked(conv, learner_p, f, caption="check this out")
        self.assertIsNone(error)
        self.assertEqual(msg.message_type, Message.TYPE_IMAGE)
        data = services.serialize_message(msg)
        self.assertIsNotNone(data["attachment"])
        self.assertEqual(data["attachment"]["kind"], "IMAGE")
        self.assertEqual(data["body"], "check this out")

    def test_an_unsupported_extension_is_rejected(self):
        conv, learner_p, _ = make_direct_conversation()
        f = SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")
        msg, error = services.post_attachment_checked(conv, learner_p, f)
        self.assertIsNone(msg)
        self.assertEqual(error["category"], "attachment")

    def test_empty_file_is_rejected(self):
        conv, learner_p, _ = make_direct_conversation()
        empty = SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf")
        msg, error = services.post_attachment_checked(conv, learner_p, empty)
        self.assertIsNone(msg)
        self.assertEqual(error["category"], "attachment")

    def test_oversized_file_is_rejected(self):
        from chat import attachments as attachment_rules
        conv, learner_p, _ = make_direct_conversation()
        too_big = SimpleUploadedFile(
            "big.pdf", b"x" * (attachment_rules.MAX_ATTACHMENT_BYTES + 1),
            content_type="application/pdf",
        )
        msg, error = services.post_attachment_checked(conv, learner_p, too_big)
        self.assertIsNone(msg)
        self.assertEqual(error["category"], "attachment")
        self.assertIn("Maximum allowed", error["reason"])

    def test_blocked_direct_thread_refuses_an_attachment_too(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        services.create_block(
            Participant.KIND_TEACHER, teacher_p.teacher_profile,
            Participant.KIND_LEARNER, learner_p.learner_profile,
        )
        f = SimpleUploadedFile("photo.png", b"data", content_type="image/png")
        msg, error = services.post_attachment_checked(conv, learner_p, f)
        self.assertIsNone(msg)
        self.assertEqual(error["category"], "blocked")

    def test_deleted_message_hides_its_attachment(self):
        conv, learner_p, _ = make_direct_conversation()
        f = SimpleUploadedFile("photo.png", b"data", content_type="image/png")
        msg, _ = services.post_attachment_checked(conv, learner_p, f)
        services.soft_delete_message(msg, participant=learner_p)
        msg.refresh_from_db()
        data = services.serialize_message(msg)
        self.assertIsNone(data["attachment"])
        self.assertTrue(data["deleted"])

    def test_shared_files_endpoint_lists_attachments_and_hides_deleted_ones(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        f1 = SimpleUploadedFile("a.png", b"data", content_type="image/png")
        f2 = SimpleUploadedFile("b.pdf", b"data", content_type="application/pdf")
        msg1, _ = services.post_attachment_checked(conv, learner_p, f1)
        msg2, _ = services.post_attachment_checked(conv, teacher_p, f2)
        services.soft_delete_message(msg2, participant=teacher_p)

        user, auth = learner_p.learner_profile.account, _auth_for(Participant.KIND_LEARNER, learner_p.learner_profile)
        response = _call(chat_views.ConversationAttachmentUploadView, "get", "/x", user, auth, conversation_id=conv.id)
        self.assertEqual(response.status_code, 200)
        names = [f["name"] for f in response.data]
        self.assertIn("a.png", names)
        self.assertNotIn("b.pdf", names)


# ===========================================================================
# Stage C — course hub composition
# ===========================================================================

class CourseHubTest(TestCase):

    def test_members_endpoint_lists_learner_and_teacher_with_roles(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_room(course.id)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner)
        services._attach_participant(conv, Participant.KIND_TEACHER, teacher)

        user, auth = learner.account, _auth_for(Participant.KIND_LEARNER, learner)
        response = _call(chat_views.ConversationMembersView, "get", "/x", user, auth, conversation_id=conv.id)
        self.assertEqual(response.status_code, 200)
        kinds = sorted(m["kind"] for m in response.data)
        self.assertEqual(kinds, ["LEARNER", "TEACHER"])

    def test_resources_composes_over_existing_study_materials(self):
        from courses.models import Chapter
        from materials.models import StudyMaterial

        learner, teacher, course = enrolled_learner_and_teacher()
        subject = course.subjects.first()
        chapter = Chapter.objects.create(subject=subject, title="Ch 1")
        StudyMaterial.objects.create(chapter=chapter, title="Notes", uploaded_by=teacher.user)

        out = services.course_resources(course.id)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Notes")
        self.assertEqual(out[0]["chapter"], "Ch 1")

    def test_resources_view_denies_a_non_member(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        outsider = make_learner()
        response = _call(
            chat_views.CourseResourcesView, "get", "/x", outsider.account,
            _auth_for(Participant.KIND_LEARNER, outsider), course_id=course.id,
        )
        self.assertEqual(response.status_code, 403)


# ===========================================================================
# Stage D — Announcements (BROADCAST wiring)
# ===========================================================================

class AnnouncementTest(TestCase):

    def test_teacher_can_post_student_cannot(self):
        learner, teacher, course = enrolled_learner_and_teacher()

        t_resp = _call(
            chat_views.CourseAnnouncementsView, "post", "/x", teacher.user,
            _auth_for(Participant.KIND_TEACHER, teacher),
            data={"body": "Midterm moved to Friday."}, course_id=course.id,
        )
        self.assertEqual(t_resp.status_code, 201)
        self.assertEqual(t_resp.data["message_type"], Message.TYPE_ANNOUNCEMENT)

        s_resp = _call(
            chat_views.CourseAnnouncementsView, "post", "/x", learner.account,
            _auth_for(Participant.KIND_LEARNER, learner),
            data={"body": "can I ask a question"}, course_id=course.id,
        )
        self.assertEqual(s_resp.status_code, 403)

    def test_feed_is_readable_by_an_enrolled_student(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_announcements(course.id)
        services._attach_participant(conv, Participant.KIND_TEACHER, teacher)
        t_p = services.participant_for(conv, Participant.KIND_TEACHER, teacher)
        services.post_message(conv, t_p, "Welcome to the course!")

        response = _call(
            chat_views.CourseAnnouncementsView, "get", "/x", learner.account,
            _auth_for(Participant.KIND_LEARNER, learner), course_id=course.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["messages"]), 1)
        self.assertEqual(response.data["conversation"]["category"], "announcements")

    def test_course_room_and_announcements_coexist_without_constraint_clash(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        room = services.ensure_course_room(course.id)
        broadcast = services.ensure_course_announcements(course.id)
        self.assertNotEqual(room.id, broadcast.id)
        self.assertEqual(room.kind, Conversation.KIND_ROOM)
        self.assertEqual(broadcast.kind, Conversation.KIND_BROADCAST)

    def test_a_second_call_does_not_create_a_duplicate_broadcast_room(self):
        _, _, course = enrolled_learner_and_teacher()
        a = services.ensure_course_announcements(course.id)
        b = services.ensure_course_announcements(course.id)
        self.assertEqual(a.id, b.id)


# ===========================================================================
# Stage D — Academic Support tickets
# ===========================================================================

class SupportTicketTest(TestCase):

    def test_create_ticket_posts_the_first_message(self):
        learner = make_learner()
        ticket, error = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Can't access my course",
            SupportTicket.CATEGORY_TECHNICAL, "It just spins forever.",
        )
        self.assertIsNone(error)
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        msgs = list(ticket.conversation.messages.all())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].body, "It just spins forever.")

    def test_empty_first_message_is_rejected_before_any_row_is_created(self):
        learner = make_learner()
        before = SupportTicket.objects.count()
        ticket, error = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Subject", SupportTicket.CATEGORY_OTHER, "   ",
        )
        self.assertIsNone(ticket)
        self.assertIsNotNone(error)
        self.assertEqual(SupportTicket.objects.count(), before)

    def test_staff_reply_attaches_a_staff_participant_and_notifies(self):
        learner = make_learner()
        ticket, _ = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Billing question",
            SupportTicket.CATEGORY_BILLING, "Why was I charged twice?",
        )
        staff_user = make_user(username="support_agent")
        staff_user.is_staff = True
        staff_user.save(update_fields=["is_staff"])

        response = _call(
            chat_views.SupportTicketReplyView, "post", "/x", staff_user, {},
            data={"message": "Looking into this now!"}, ticket_id=ticket.id,
        )
        self.assertEqual(response.status_code, 201)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.STATUS_IN_PROGRESS)

        staff_participant = Participant.objects.get(conversation=ticket.conversation, kind=Participant.KIND_STAFF)
        self.assertEqual(staff_participant.staff_user_id, staff_user.id)
        # Reusing the identity registry's KIND_SYSTEM letter, per design.
        identity = Identity.objects.get(kind=Identity.KIND_SYSTEM, profile_id=str(staff_user.id))
        self.assertEqual(staff_participant.identity_key(), identity.key)

    def test_a_stranger_cannot_read_someone_elses_ticket(self):
        learner = make_learner()
        ticket, _ = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Subject", SupportTicket.CATEGORY_OTHER, "help",
        )
        stranger = make_learner()
        response = _call(
            chat_views.SupportTicketMessagesView, "get", "/x", stranger.account,
            _auth_for(Participant.KIND_LEARNER, stranger), ticket_id=ticket.id,
        )
        self.assertEqual(response.status_code, 403)

    def test_requester_can_close_their_own_ticket(self):
        learner = make_learner()
        ticket, _ = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Subject", SupportTicket.CATEGORY_OTHER, "help",
        )
        response = _call(
            chat_views.SupportTicketCloseView, "post", "/x", learner.account,
            _auth_for(Participant.KIND_LEARNER, learner), ticket_id=ticket.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], SupportTicket.STATUS_CLOSED)


# ===========================================================================
# Stage D — Administrator Console
# ===========================================================================

class AdminConsoleTest(TestCase):

    def _admin(self):
        u = make_user(username="admin_user")
        u.is_staff = True
        u.save(update_fields=["is_staff"])
        return u

    def test_non_admin_is_forbidden(self):
        conv, learner_p, _ = make_direct_conversation()
        response = _call(
            chat_views.AdminReportsQueueView, "get", "/x",
            learner_p.learner_profile.account, {},
        )
        self.assertEqual(response.status_code, 403)

    def test_resolve_report_remove_message(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "inappropriate text")
        report = Report.objects.create(
            conversation=conv, message=msg, reporter=learner_p,
            target_identity=teacher_p.identity_key(), reason=Report.REASON_INAPPROPRIATE,
        )
        admin = self._admin()
        response = _call(
            chat_views.AdminResolveReportView, "post", "/x", admin, {},
            data={"action": "remove_message", "note": "policy violation"},
            report_id=report.id,
        )
        self.assertEqual(response.status_code, 200)
        msg.refresh_from_db()
        self.assertIsNotNone(msg.deleted_at)
        self.assertEqual(msg.deleted_reason, "policy violation")
        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_ACTION_TAKEN)
        self.assertEqual(report.resolved_by_id, admin.id)

    def test_resolve_report_suspend_user(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, teacher_p, "text")
        report = Report.objects.create(
            conversation=conv, message=msg, reporter=learner_p,
            target_identity=teacher_p.identity_key(), reason=Report.REASON_SPAM,
        )
        admin = self._admin()
        response = _call(
            chat_views.AdminResolveReportView, "post", "/x", admin, {},
            data={"action": "suspend_user"}, report_id=report.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(services.is_suspended(teacher_p))

    def test_broadcast_reaches_all_active_students(self):
        learner1 = make_learner()
        learner2 = make_learner()
        make_teacher()  # a teacher must NOT be counted for "all_students"
        admin = self._admin()
        response = _call(
            chat_views.AdminBroadcastView, "post", "/x", admin, {},
            data={"audience": "all_students", "title": "Platform maintenance", "body": "Tonight at 10pm."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["recipients"], 2)

    def test_logs_view_reports_open_counts(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        msg = services.post_message(conv, learner_p, "hi")
        Report.objects.create(conversation=conv, message=msg, reporter=learner_p,
                               target_identity=teacher_p.identity_key(), reason=Report.REASON_OTHER)
        admin = self._admin()
        response = _call(chat_views.AdminLogsView, "get", "/x", admin, {})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data["open_reports"], 1)
        self.assertGreaterEqual(response.data["messages_today"], 1)


# ===========================================================================
# Stage E — comm preferences gate presence/read-receipts
# ===========================================================================

class CommPreferenceTest(TestCase):

    def test_disabling_online_status_hides_it_from_the_counterpart(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        pref = CommPreference.for_identity(teacher_p.identity_key())
        pref.show_online_status = False
        pref.save()

        data = services.serialize_conversation(conv, learner_p)
        self.assertIsNone(data["counterpart"]["online"])

    def test_default_preferences_show_status(self):
        conv, learner_p, teacher_p = make_direct_conversation()
        data = services.serialize_conversation(conv, learner_p)
        # Nobody has connected over the socket in this test, so online is
        # False (not None) — None means "hidden by preference", False means
        # "visible, and currently offline". That distinction is the point.
        self.assertFalse(data["counterpart"]["online"])


# ===========================================================================
# Category derivation (CC-004)
# ===========================================================================

class ConversationCategoryTest(TestCase):

    def test_course_room_is_category_courses(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_room(course.id)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner)
        data = services.serialize_conversation(conv, services.participant_for(conv, Participant.KIND_LEARNER, learner))
        self.assertEqual(data["category"], "courses")
        self.assertEqual(data["course"]["title"], course.title)

    def test_direct_with_faculty_categorizes_as_faculty(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        from accounts.models import TeacherProfile
        teacher.academy_status = TeacherProfile.TRACK_APPROVED
        teacher.save(update_fields=["academy_status"])
        conv = services.ensure_direct(Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher)
        me = services.participant_for(conv, Participant.KIND_LEARNER, learner)
        data = services.serialize_conversation(conv, me)
        self.assertEqual(data["category"], "faculty")

    def test_direct_between_two_learners_categorizes_as_students(self):
        from chat.models import Conversation as Conv
        l1 = make_learner()
        l2 = make_learner()
        # Bypass the DM-matrix check (SAME_ROOM_ONLY) for this pure
        # serialization test — ensure_direct() itself doesn't enforce
        # policy (views do), so this is a legitimate direct construction.
        conv = services.ensure_direct(Participant.KIND_LEARNER, l1, Participant.KIND_LEARNER, l2)
        me = services.participant_for(conv, Participant.KIND_LEARNER, l1)
        data = services.serialize_conversation(conv, me)
        self.assertEqual(data["category"], "students")


# ===========================================================================
# Outbox verb selection (Stage D — CC-015/022 notification routing)
# ===========================================================================

class ProfileEndpointTest(TestCase):

    def test_teacher_profile_includes_headline_and_courses(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        response = _call(
            chat_views.ProfileView, "get", "/x", learner.account,
            _auth_for(Participant.KIND_LEARNER, learner),
            kind=Participant.KIND_TEACHER, target_id=teacher.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["kind"], "TEACHER")
        self.assertIn(course.title, response.data["courses"])

    def test_learner_profile_is_minimal(self):
        viewer = make_learner()
        target = make_learner()
        response = _call(
            chat_views.ProfileView, "get", "/x", viewer.account,
            _auth_for(Participant.KIND_LEARNER, viewer),
            kind=Participant.KIND_LEARNER, target_id=target.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["bio"], "")
        self.assertEqual(response.data["courses"], [])

    def test_unknown_target_is_rejected(self):
        viewer = make_learner()
        response = _call(
            chat_views.ProfileView, "get", "/x", viewer.account,
            _auth_for(Participant.KIND_LEARNER, viewer),
            kind=Participant.KIND_TEACHER, target_id=uuid.uuid4(),
        )
        self.assertEqual(response.status_code, 400)
    """chat/outbox_handlers.py picks a different notifications.services
    .notify() verb depending on which KIND of conversation a message landed
    in — this is what lets a course Announcement and a Support ticket reply
    carry their own policy row (notifications/policy.py) and their own
    label, instead of both looking like generic "New message" chat pings."""

    def test_broadcast_message_notifies_with_announcement_verb(self):
        from chat import outbox_handlers
        from notifications.models import Notification

        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_announcements(course.id)
        services._attach_participant(conv, Participant.KIND_LEARNER, learner)
        t_p = services._attach_participant(conv, Participant.KIND_TEACHER, teacher)
        services.post_message(conv, t_p, "Midterm moved to Friday.")

        outbox_handlers.drain_once()

        note = Notification.objects.get(recipient=learner.account, verb="announcement.posted")
        self.assertIn("announcement", note.title.lower())
        self.assertEqual(note.payload["conversation_id"], str(conv.id))

    def test_support_reply_notifies_with_support_verb(self):
        from chat import outbox_handlers
        from notifications.models import Notification
        from chat.models import SupportTicket

        learner = make_learner()
        ticket, _ = services.create_support_ticket(
            Participant.KIND_LEARNER, learner, "Subject", SupportTicket.CATEGORY_OTHER, "help please",
        )
        staff = make_user(username="agent2")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        staff_p = services.attach_staff_participant(ticket.conversation, staff)
        services.post_message(ticket.conversation, staff_p, "On it!")

        outbox_handlers.drain_once()

        note = Notification.objects.get(recipient=learner.account, verb="support.reply")
        self.assertIn("support ticket", note.title.lower())

    def test_ordinary_direct_message_still_notifies_with_chat_verb(self):
        from chat import outbox_handlers
        from notifications.models import Notification

        conv, learner_p, teacher_p = make_direct_conversation()
        services.post_message(conv, learner_p, "hey")
        outbox_handlers.drain_once()
        self.assertTrue(
            Notification.objects.filter(
                recipient=teacher_p.teacher_profile.user, verb="chat.message",
            ).exists()
        )
