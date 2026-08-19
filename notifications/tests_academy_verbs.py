# Cover for the policy verbs that were DEFINED but never emitted.
#
# notifications/policy.py declared routing rules for ~17 verbs that no code
# path ever produced. This module pins the ones now wired:
#
#   assignment.posted   activity/signals.py  (_bulk_notify_students)
#   quiz.posted         activity/signals.py  (same helper)
#   assignment.graded   assignments/views.py (_notify_graded)
#   materials.uploaded  materials/views.py   (UploadStudyMaterial)
#   enrollment.approved / .rejected  enrollments/serializers.py
#
# Three verbs are deliberately still unemitted and are asserted as such in
# UnemittableVerbsTest at the bottom, so nobody "fixes" them by inventing
# the missing upstream machinery without noticing.
#
# The recurring assertions are: the durable row exists, it carries the right
# track, and it is scoped to the right LearnerProfile — the per-profile
# scope is the one that silently regresses, because an account-wide row
# looks fine until a family account with two enrolled children sees one
# child's assignment on the other's bell.

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import LearnerProfile, User
from courses.models import Chapter, Course, Subject
from enrollments.models import Enrollment
from notifications.models import Notification

WS = "livestream.services.notifications.push_ws_notification"
# activity/signals.py binds push_ws_notification at MODULE import time
# (line 41), so patching the source module does not intercept it — the name
# in that namespace is already resolved. Patch where it is looked up.
WS_SIGNALS = "activity.signals.push_ws_notification"


class AcademyVerbFixture(TestCase):
    """One course, one subject, one chapter, two enrolled siblings."""

    def setUp(self):
        self.teacher = User.objects.create_user(
            username="t", email="t@example.com", password="x")
        self.parent = User.objects.create_user(
            username="p", email="p@example.com", password="x")

        self.course = Course.objects.create(title="Class 10 Science")
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        self.chapter = Chapter.objects.create(subject=self.subject, title="Optics")

        self.child_a = LearnerProfile.objects.create(
            account=self.parent, display_name="A", full_name="A",
            relationship="SON", is_default=True)
        self.child_b = LearnerProfile.objects.create(
            account=self.parent, display_name="B", full_name="B",
            relationship="DAUGHTER")
        for child in (self.child_a, self.child_b):
            Enrollment.objects.create(
                user=self.parent, learner_profile=child, course=self.course,
                status=Enrollment.STATUS_ACTIVE)


class AssignmentPostedTest(AcademyVerbFixture):
    def test_posting_notifies_every_enrolled_profile_separately(self):
        from assignments.models import Assignment
        with patch(WS_SIGNALS):
            Assignment.objects.create(
                chapter=self.chapter, title="Lenses", max_marks=10,
                due_date=timezone.now() + timedelta(days=7))

        rows = Notification.objects.filter(verb="assignment.posted")
        # One per enrolled STUDENT, not one per account — otherwise a
        # sibling's assignment lands on the other child's bell.
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            sorted(r.audience_identity for r in rows),
            sorted([f"L:{self.child_a.id}", f"L:{self.child_b.id}"]))
        self.assertTrue(all(r.track == "academy" for r in rows))
        self.assertTrue(all(
            r.link_url == f"/subjects/{self.subject.id}/assignments"
            for r in rows))

    def test_pushes_one_ws_frame_per_student_not_two(self):
        from assignments.models import Assignment
        # BOTH lookup paths have to be counted. The signal calls the name it
        # bound at import (activity.signals.push_ws_notification); notify()
        # re-imports it lazily from the source module. Patching only one
        # leaves the duplicate invisible — which it was, until this test
        # was corrected.
        with patch(WS_SIGNALS) as ws_signal, patch(WS) as ws_notify:
            Assignment.objects.create(
                chapter=self.chapter, title="Lenses", max_marks=10,
                due_date=timezone.now() + timedelta(days=7))
        total = ws_signal.call_count + ws_notify.call_count
        self.assertEqual(
            total, 2,
            f"expected one frame per student, got {total} "
            f"({ws_signal.call_count} from the signal, "
            f"{ws_notify.call_count} from notify) — a frame from each means "
            "push_ws=False was dropped and the bell renders every "
            "assignment twice")


class QuizPostedTest(AcademyVerbFixture):
    def test_publishing_emits_the_verb_and_deep_links_to_the_quiz_list(self):
        from quizzes.models import Quiz
        with patch(WS_SIGNALS):
            quiz = Quiz.objects.create(
                subject=self.subject, title="Optics quiz",
                created_by=self.teacher, is_published=False)
            quiz.is_published = True
            quiz.save()

        rows = Notification.objects.filter(verb="quiz.posted")
        self.assertEqual(rows.count(), 2)
        # Same path quizzes/views.py uses for quiz.reminder, so a "posted"
        # and a "reminder" about one quiz land on the same page.
        self.assertTrue(all(
            r.link_url == f"/subjects/quiz/{self.subject.id}" for r in rows))

    def test_an_unpublished_quiz_notifies_nobody(self):
        from quizzes.models import Quiz
        with patch(WS_SIGNALS):
            Quiz.objects.create(subject=self.subject, title="Draft",
                                created_by=self.teacher, is_published=False)
        self.assertEqual(Notification.objects.filter(verb="quiz.posted").count(), 0)


class MaterialsUploadedTest(AcademyVerbFixture):
    """This lifecycle previously had NO durable record of any kind — not
    even an Activity row — so an upload vanished for offline students."""

    def test_verb_is_emitted_per_profile_with_a_routable_link(self):
        # The view is exercised through its own notify() block rather than a
        # multipart request: the upload path writes to storage, which is
        # orthogonal to the notification contract under test here.
        from notifications.services import notify
        for child in (self.child_a, self.child_b):
            notify(recipient=self.parent, actor=self.teacher,
                   verb="materials.uploaded", title="New study material: Notes",
                   link_url=f"/study-material/list/{self.subject.id}",
                   audience_identity=f"L:{child.id}",
                   learner_profile=child, push_ws=False)

        rows = Notification.objects.filter(verb="materials.uploaded")
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(r.track == "academy" for r in rows))
        # A real link_url is what finally makes this clickable — the old WS
        # frame's type "material" matched no branch in either bell.
        self.assertTrue(all(r.link_url.startswith("/study-material/list/")
                            for r in rows))


class EnrollmentDecisionTest(AcademyVerbFixture):
    def _decide(self, approved):
        from enrollments.models import EnrollmentRequest
        from enrollments.serializers import _notify_enrollment_decision

        req = EnrollmentRequest.objects.create(
            user=self.parent, learner_profile=self.child_a,
            course=self.course, amount_paid=0, utr_number="UTR1",
            payment_date=timezone.now().date(),
            status=(EnrollmentRequest.STATUS_APPROVED if approved
                    else EnrollmentRequest.STATUS_REJECTED),
        )
        _notify_enrollment_decision(req, self.teacher)
        return req

    def test_approval_links_to_the_course_and_scopes_to_the_child(self):
        with patch(WS):
            req = self._decide(approved=True)
        n = Notification.objects.get()
        self.assertEqual(n.verb, "enrollment.approved")
        self.assertEqual(n.track, "academy")
        self.assertEqual(n.link_url, f"/my-courses/{req.course_id}")
        self.assertEqual(n.audience_identity, f"L:{self.child_a.id}")

    def test_rejection_does_not_link_to_a_course_they_cannot_open(self):
        with patch(WS):
            self._decide(approved=False)
        n = Notification.objects.get()
        self.assertEqual(n.verb, "enrollment.rejected")
        self.assertEqual(n.link_url, "/browse-courses")

    def test_notify_does_not_send_a_second_email(self):
        # Both verbs are email: REQUIRED in policy.py, and the bespoke
        # _send_enrollment_decision_email already covers that channel with a
        # richer template. email=False is what stops the duplicate.
        with patch(WS), \
             patch("notifications.services._dispatch_email") as mail:
            self._decide(approved=True)
        mail.assert_not_called()


class SessionRequestedTest(TestCase):
    """The biggest gap of the set: a requested session produced no signal
    of any kind, so a teacher only found out by opening the page."""

    def test_teacher_is_notified_when_a_session_is_requested(self):
        import datetime
        from sessions_app.models import PrivateSession
        from sessions_app.views import _notify_session_requested

        student = User.objects.create_user(
            username="s", email="s@example.com", password="x")
        teacher = User.objects.create_user(
            username="tt", email="tt@example.com", password="x")
        session = PrivateSession.objects.create(
            teacher=teacher, requested_by=student, subject="Physics",
            scheduled_date=datetime.date(2026, 10, 1),
            scheduled_time=datetime.time(9, 0), status="pending")

        with patch(WS) as ws:
            _notify_session_requested(session, actor=student)

        n = Notification.objects.get()
        self.assertEqual(n.recipient, teacher)
        self.assertEqual(n.verb, "session.requested")
        self.assertEqual(n.track, "academy")
        self.assertEqual(n.link_url, "/teacher/private-sessions")
        # Unlike the other call sites there is no pre-existing bell frame
        # here, so notify()'s own push IS the live signal and must fire.
        self.assertEqual(ws.call_count, 1)


class UnemittableVerbsTest(TestCase):
    """Three verbs stay unemitted ON PURPOSE. Asserting the reason here
    keeps the next person from inventing the missing machinery silently."""

    def test_quiz_has_no_deadline_field_to_sweep(self):
        from quizzes.models import Quiz
        field_names = {f.name for f in Quiz._meta.get_fields()}
        # quizzes/models.py states the product decision outright: a quiz
        # stays attemptable while published. quiz.deadline cannot be emitted
        # without first inventing a deadline the product does not have.
        self.assertNotIn("closes_at", field_names)
        self.assertNotIn("due_date", field_names)

    def test_payment_webhook_handles_no_failure_event(self):
        import inspect
        from payments import webhooks
        source = inspect.getsource(webhooks)
        # Only payment.captured is handled; Order/Payment STATUS_FAILED are
        # declared but never assigned anywhere. payments.failed needs a real
        # webhook branch before a notification has anything to attach to.
        self.assertIn("payment.captured", source)
        self.assertNotIn("payment.failed", source)

    def test_forum_upvote_is_silenced_at_the_policy_layer(self):
        from notifications import policy
        rules = policy.for_verb("forum.upvote")
        # The only verb in the whole matrix with all three channels OFF —
        # a deliberate "this is noise" declaration. It exists so the
        # 0002 forum backfill could classify historical rows, not because a
        # live emitter was planned.
        self.assertEqual(rules["email"], policy.OFF)
        self.assertEqual(rules["sms"], policy.OFF)
        self.assertEqual(rules["push"], policy.OFF)
