# M3 §10 — the policy engine: capability-provider registry + fail-closed
# room join, the DM matrix, and can_post's structural gate. Also one
# view-level test confirming StartDirectView actually enforces the matrix,
# not just that the policy function is correct in isolation.
from accounts.models import Identity
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import PermissionDenied

from chat import policy, services
from chat.models import Conversation, Participant
from chat.views import StartDirectView

from .factories import (
    enrolled_learner_and_teacher, make_course, make_learner, make_subject,
    make_teacher, assign_teacher_to_subject, make_active_subscription,
)


class ProviderRegistrationTest(TestCase):
    """chat/apps.py's ready() should already have registered "course" by
    the time any test runs (AppConfig.ready() fires once at Django
    startup) — this just confirms that wiring exists, since every other
    test in this file relies on it implicitly."""

    def test_course_provider_is_registered(self):
        self.assertIsNotNone(policy.get_provider("course"))

    def test_unregistered_context_type_has_no_provider(self):
        self.assertIsNone(policy.get_provider("counseling_case"))


class CanJoinRoomFailClosedTest(TestCase):

    def test_unregistered_context_type_denies_regardless_of_who_asks(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        allowed = policy.can_join_room("counseling_case", "case-1", Participant.KIND_LEARNER, learner)
        self.assertFalse(allowed)

    def test_registered_course_provider_allows_an_enrolled_learner(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        allowed = policy.can_join_room("course", str(course.id), Participant.KIND_LEARNER, learner)
        self.assertTrue(allowed)

    def test_registered_course_provider_denies_a_non_enrolled_learner(self):
        course = make_course()
        stranger = make_learner()  # no subscription to `course`
        allowed = policy.can_join_room("course", str(course.id), Participant.KIND_LEARNER, stranger)
        self.assertFalse(allowed)

    def test_provider_exception_fails_closed(self):
        def _boom(kind, obj, context_id):
            raise RuntimeError("simulated provider bug")

        policy.register_provider("flaky", _boom)
        try:
            allowed = policy.can_join_room("flaky", "x", Participant.KIND_LEARNER, make_learner())
            self.assertFalse(allowed)
        finally:
            del policy._PROVIDERS["flaky"]  # don't leak into other tests


class DmMatrixTest(TestCase):

    def test_learner_teacher_allowed_when_sharing_an_active_course(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_learner_teacher_denied_without_a_shared_course(self):
        learner = make_learner()
        teacher = make_teacher()  # teaches nothing, learner enrolled in nothing
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_learner_can_message_approved_faculty_with_no_shared_course(self):
        """Regression: the "Explore Experts"/people directory lists every
        approved-faculty teacher regardless of subject assignment, but the
        DM rule used to require a shared active course — so the directory's
        own "Message" button 403'd for anyone not already assigned. Approved
        faculty are messageable on directory membership alone now."""
        from accounts.models import TeacherProfile

        learner = make_learner()
        teacher = make_teacher()  # no shared course with `learner`
        teacher.academy_status = TeacherProfile.TRACK_APPROVED
        teacher.save(update_fields=["academy_status"])

        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_learner_can_message_listed_expert_with_no_shared_course(self):
        from skills.models import ExpertProfile

        learner = make_learner()
        teacher = make_teacher()  # no shared course with `learner`
        ExpertProfile.objects.create(
            teacher_profile=teacher, headline="Guest expert", is_listed=True,
        )

        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_learner_still_denied_for_unlisted_unapproved_teacher_with_no_shared_course(self):
        """The relaxed rule only widens access for teachers the directory
        would actually surface — a teacher who is neither approved faculty
        nor a listed expert, and shares no course with the learner, is
        still not messageable."""
        learner = make_learner()
        teacher = make_teacher()  # locked/pending academy_status, no expert profile
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        self.assertFalse(allowed)

    def test_learner_learner_denied_without_a_shared_room(self):
        a, b = make_learner(), make_learner()
        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, a, Participant.KIND_LEARNER, b,
        )
        self.assertFalse(allowed)

    def test_learner_learner_allowed_once_sharing_a_room(self):
        a, b = make_learner(), make_learner()
        course = make_course()
        room = services.ensure_course_room(course.id, title=course.title)
        services._attach_participant(room, Participant.KIND_LEARNER, a)
        services._attach_participant(room, Participant.KIND_LEARNER, b)

        allowed, reason = policy.can_start_dm(
            Participant.KIND_LEARNER, a, Participant.KIND_LEARNER, b,
        )
        self.assertTrue(allowed)

    def test_teacher_teacher_always_allowed(self):
        t1, t2 = make_teacher(), make_teacher()
        allowed, reason = policy.can_start_dm(
            Participant.KIND_TEACHER, t1, Participant.KIND_TEACHER, t2,
        )
        self.assertTrue(allowed)

    def test_unspecified_pairs_default_to_never(self):
        """Every pair touching C (counsellor), R (recruiter), or S (system)
        defaults to NEVER — exercised directly at the matrix level since
        chat.Participant doesn't support these as real participant kinds
        yet, so can_start_dm() itself can't be called with them."""
        self.assertEqual(
            policy._dm_rule(Identity.KIND_LEARNER, Identity.KIND_COUNSELOR), policy.DM_NEVER,
        )
        self.assertEqual(
            policy._dm_rule(Identity.KIND_TEACHER, Identity.KIND_RECRUITER), policy.DM_NEVER,
        )
        self.assertEqual(
            policy._dm_rule(Identity.KIND_RECRUITER, Identity.KIND_RECRUITER), policy.DM_NEVER,
        )
        self.assertEqual(
            policy._dm_rule(Identity.KIND_SYSTEM, Identity.KIND_LEARNER), policy.DM_NEVER,
        )


class CanPostStructuralGateTest(TestCase):

    def test_frozen_conversation_refuses_post(self):
        learner = make_learner()
        teacher = make_teacher()
        conv = services.ensure_direct(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        conv.is_frozen = True
        conv.save(update_fields=["is_frozen"])
        p = services.participant_for(conv, Participant.KIND_LEARNER, learner)

        allowed, reason = policy.can_post(conv, p)
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_broadcast_conversation_is_read_only_for_everyone(self):
        conv = Conversation.objects.create(kind=Conversation.KIND_BROADCAST)
        learner = make_learner()
        p = services._attach_participant(conv, Participant.KIND_LEARNER, learner)

        allowed, reason = policy.can_post(conv, p)
        self.assertFalse(allowed)

    def test_ordinary_room_allows_posting(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        conv = services.ensure_course_room(course.id, title=course.title)
        p = services._attach_participant(conv, Participant.KIND_LEARNER, learner)

        allowed, reason = policy.can_post(conv, p)
        self.assertTrue(allowed)

    def test_frozen_gate_reaches_post_message_checked(self):
        """Confirms the actual wiring into post_message_checked(), not
        just the policy function in isolation — and that it runs BEFORE
        moderation (an otherwise-clean message is still refused)."""
        learner = make_learner()
        teacher = make_teacher()
        conv = services.ensure_direct(
            Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
        )
        conv.is_frozen = True
        conv.save(update_fields=["is_frozen"])
        p = services.participant_for(conv, Participant.KIND_LEARNER, learner)

        msg, err = services.post_message_checked(conv, p, "a perfectly polite message")
        self.assertIsNone(msg)
        self.assertEqual(err["category"], "policy")


class StartDirectViewWiringTest(TestCase):
    """One integration-level test: the DM matrix must actually be enforced
    by the view, not just callable in isolation."""

    def setUp(self):
        self.factory = APIRequestFactory()

    def _post_as(self, user, auth, data):
        request = self.factory.post("/api/chat/start/", data, format="json")
        force_authenticate(request, user=user, token=auth)
        return StartDirectView.as_view()(request)

    def test_denied_dm_returns_403_through_the_real_view(self):
        learner = make_learner()
        teacher = make_teacher()  # no shared course with `learner`
        response = self._post_as(
            learner.account,
            auth={"context": "learner", "active_profile": str(learner.id)},
            data={"target_kind": "TEACHER", "target_id": str(teacher.id)},
        )
        self.assertEqual(response.status_code, 403)

    def test_allowed_dm_creates_the_conversation_through_the_real_view(self):
        learner, teacher, course = enrolled_learner_and_teacher()
        response = self._post_as(
            learner.account,
            auth={"context": "learner", "active_profile": str(learner.id)},
            data={"target_kind": "TEACHER", "target_id": str(teacher.id)},
        )
        self.assertEqual(response.status_code, 201)
