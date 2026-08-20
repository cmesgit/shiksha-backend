"""
Tests for the media_security rules added for forum/attachments and
session_files/ — both previously had NO entry in _RULES at all, which
`is_authorized`'s fallback resolves to staff-only. That meant every forum
attachment and every live-session shared file 404'd for every non-staff
user, including whoever had just uploaded it or was legitimately in the
room — a live functional break, not just a hardening gap.
"""
from datetime import date, time, timedelta

from django.test import TestCase, RequestFactory
from django.utils import timezone

from accounts.models import Role, User, UserRole
from config.media_security import is_authorized, is_public
from forum.models import Attachment, ForumPost
from sessions_app.models import (
    GroupSession, GroupSessionParticipant, PrivateSession,
    PrivateSessionFile, SessionFile, SessionParticipant,
)


def _req(user):
    rf = RequestFactory()
    req = rf.get("/")
    req.user = user
    return req


class ForumAttachmentMediaSecurityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="fauthor", email="fauthor@test.com", password="x", is_verified=True,
        )
        cls.outsider = User.objects.create_user(
            username="foutsider", email="foutsider@test.com", password="x", is_verified=True,
        )
        cls.staff = User.objects.create_user(
            username="fstaff", email="fstaff@test.com", password="x", is_staff=True,
        )
        cls.post = ForumPost.objects.create(
            author=cls.author, title="A question", content="body",
            kind=ForumPost.KIND_QUESTION,
        )
        cls.attachment = Attachment.objects.create(
            post=cls.post, file="forum/attachments/1/photo.png",
            kind=Attachment.KIND_IMAGE, original_name="photo.png", uploaded_by=cls.author,
        )

    def test_anyone_can_read_an_attachment_on_a_live_post(self):
        # Forum reads are AllowAny end to end — no rule at all used to mean
        # this fell to the staff-only fallback, breaking it for everyone.
        self.assertTrue(is_authorized(_req(self.outsider), self.attachment.file.name))
        self.assertTrue(is_authorized(_req(self.author), self.attachment.file.name))
        self.assertTrue(is_authorized(_req(self.staff), self.attachment.file.name))

    def test_removed_post_attachment_is_denied(self):
        self.post.is_removed = True
        self.post.save(update_fields=["is_removed"])
        self.assertFalse(is_authorized(_req(self.outsider), self.attachment.file.name))
        # Staff can still see it (moderation review).
        self.assertTrue(is_authorized(_req(self.staff), self.attachment.file.name))

    def test_unknown_forum_path_is_denied(self):
        self.assertFalse(is_authorized(_req(self.outsider), "forum/attachments/999/nope.png"))

    def test_anonymous_visitor_can_read_a_live_post_attachment(self):
        # Forum reads are genuinely AllowAny — real anonymous visitors browse
        # threads, so an anonymous user must be able to fetch its attachments
        # too, not just other logged-in accounts.
        from django.contrib.auth.models import AnonymousUser
        self.assertTrue(is_authorized(_req(AnonymousUser()), self.attachment.file.name))


class AgreementLetterMediaSecurityTest(TestCase):
    """The BLANK admin-imported agreement template vs an individual's SIGNED
    copy. These live under confusingly similar prefixes and have opposite
    visibility, so pin both — getting it backwards would either break signup
    (template unreadable before the account exists) or expose every faculty
    member's signed document."""

    def test_blank_imported_template_is_public(self):
        # A prospective applicant must read/download this during signup,
        # BEFORE any account exists. Contains nobody's data.
        self.assertTrue(is_public("agreements/letters/faculty-agreement-v1.pdf"))
        from django.contrib.auth.models import AnonymousUser
        self.assertTrue(
            is_authorized(_req(AnonymousUser()), "agreements/letters/faculty-agreement-v1.pdf")
        )

    def test_an_individuals_signed_copy_is_not_public(self):
        self.assertFalse(is_public("teachers/agreements/signed-by-someone.pdf"))
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(
            is_authorized(_req(AnonymousUser()), "teachers/agreements/signed-by-someone.pdf")
        )

    def test_the_longer_signed_prefix_is_not_shadowed_by_the_public_one(self):
        # "agreements/letters/" being PUBLIC must never leak into
        # "teachers/agreements/" — different trees, but an easy rule to
        # mis-write. Same specificity trap this module's docstring warns about.
        self.assertTrue(is_public("agreements/letters/x.pdf"))
        self.assertFalse(is_public("teachers/agreements/x.pdf"))
        self.assertFalse(is_public("teachers/certificates/x.pdf"))


class SessionFileMediaSecurityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.host = User.objects.create_user(
            username="shost", email="shost@test.com", password="x",
        )
        UserRole.objects.create(
            user=cls.host, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=True,
        )
        cls.in_room = User.objects.create_user(
            username="sinroom", email="sinroom@test.com", password="x",
        )
        cls.outsider = User.objects.create_user(
            username="soutsider", email="soutsider@test.com", password="x",
        )
        cls.staff = User.objects.create_user(
            username="sstaff", email="sstaff@test.com", password="x", is_staff=True,
        )

        now = timezone.now()
        cls.group_session = GroupSession.objects.create(
            host=cls.host, subject_name="Physics", course_title="C10",
            topic="Topic", scheduled_date=(now + timedelta(days=1)).date(),
            scheduled_time=time(15, 0), duration_minutes=45, status="scheduled",
        )
        GroupSessionParticipant.objects.create(session=cls.group_session, user=cls.in_room)
        cls.group_file = SessionFile.objects.create(
            session=cls.group_session, uploaded_by=cls.host,
            file="session_files/2026/08/20/notes.pdf",
            original_name="notes.pdf", expires_at=now + timedelta(days=7),
        )

        cls.private_session = PrivateSession.objects.create(
            teacher=cls.host, requested_by=cls.in_room, subject="Mathematics",
            scheduled_date=date.today() + timedelta(days=1), scheduled_time=time(14, 0),
            duration_minutes=60, session_type="one_on_one", group_strength=1, status="pending",
        )
        SessionParticipant.objects.create(session=cls.private_session, user=cls.in_room, role="student")
        cls.private_file = PrivateSessionFile.objects.create(
            session=cls.private_session, uploaded_by=cls.host,
            file="session_files/2026/08/20/homework.pdf",
            original_name="homework.pdf", expires_at=now + timedelta(days=7),
        )

    def test_group_session_host_and_room_member_can_read(self):
        self.assertTrue(is_authorized(_req(self.host), self.group_file.file.name))
        self.assertTrue(is_authorized(_req(self.in_room), self.group_file.file.name))

    def test_group_session_outsider_is_denied(self):
        self.assertFalse(is_authorized(_req(self.outsider), self.group_file.file.name))

    def test_private_session_teacher_and_requester_can_read(self):
        self.assertTrue(is_authorized(_req(self.host), self.private_file.file.name))
        self.assertTrue(is_authorized(_req(self.in_room), self.private_file.file.name))

    def test_private_session_outsider_is_denied(self):
        self.assertFalse(is_authorized(_req(self.outsider), self.private_file.file.name))

    def test_staff_can_read_either(self):
        self.assertTrue(is_authorized(_req(self.staff), self.group_file.file.name))
        self.assertTrue(is_authorized(_req(self.staff), self.private_file.file.name))

    def test_unknown_session_file_path_is_denied(self):
        self.assertFalse(is_authorized(_req(self.outsider), "session_files/2026/08/20/ghost.pdf"))
