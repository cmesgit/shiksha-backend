"""
Regression cover for the retake/answer-key and timer fixes:

- Unlimited retakes are intentional (StartQuizView), but the result endpoint
  used to return the full correct_choice + explanation on every attempt —
  combined, that let a student read the key on attempt 1 and resubmit for a
  free 100% on attempt 2. QuizResultView now gates that behind
  Quiz.reveal_answers_after.
- time_limit_minutes was enforced only by a client-side localStorage
  countdown a student could clear to reset their own clock. SubmitQuizView
  now enforces the same deadline server-side.
- StudentQuizSubjectsView leaked sibling profiles' subject/teacher metadata
  by scoping on the account instead of the active learner profile.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course, Subject
from enrollments.models import Subscription
from quizzes.models import Quiz, Question, Choice, QuizAttempt, StudentAnswer


class QuizRetakeAndTimerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(username="qz_t", email="qz_t@test.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=True)

        cls.student = User.objects.create_user(
            username="qz_s", email="qz_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(user=cls.student, role=Role.objects.get(name="STUDENT"), is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(account=cls.student, display_name="S", is_default=True)

        cls.course = Course.objects.create(title="Chem")
        cls.subject = Subject.objects.create(course=cls.course, name="Chemistry")
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )

        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Quick check",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, time_limit_minutes=10,
        )
        cls.question = Question.objects.create(
            quiz=cls.quiz, text="2+2?", marks=1, order=0,
        )
        cls.right = Choice.objects.create(question=cls.question, text="4", is_correct=True)
        cls.wrong = Choice.objects.create(question=cls.question, text="5", is_correct=False)

    def client_as_student(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _start_and_submit(self, client, choice):
        start = client.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(start.status_code, 200, start.content)
        submit = client.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": [{"question": str(self.question.id), "selected_choice": str(choice.id)}]},
            format="json",
        )
        return submit

    def test_start_returns_server_authoritative_started_at_and_expires_at(self):
        c = self.client_as_student()
        r = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNotNone(r.data.get("started_at"))
        self.assertIsNotNone(r.data.get("expires_at"))

    def test_first_attempt_reveals_the_answer_key(self):
        c = self.client_as_student()
        submit = self._start_and_submit(c, self.wrong)
        self.assertEqual(submit.status_code, 200, submit.content)

        result = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.data["answers_revealed"])
        self.assertEqual(result.data["questions"][0]["correct_choice"], "4")

    def test_second_attempt_hides_the_answer_key_but_keeps_score(self):
        c = self.client_as_student()
        self._start_and_submit(c, self.wrong)  # attempt 1 — burns the reveal
        submit2 = self._start_and_submit(c, self.right)  # attempt 2
        self.assertEqual(submit2.status_code, 200, submit2.content)

        result = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.data["attempt_number"], 2)
        self.assertFalse(result.data["answers_revealed"])
        self.assertEqual(result.data["questions"][0]["correct_choice"], "")
        self.assertEqual(result.data["questions"][0]["explanation"], "")
        # Score/correctness must still be visible — only the key is hidden.
        self.assertTrue(result.data["questions"][0]["is_correct"])
        self.assertEqual(result.data["score"], 1)

    def test_submitting_past_the_deadline_is_rejected(self):
        c = self.client_as_student()
        start = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(start.status_code, 200, start.content)

        attempt = QuizAttempt.objects.get(id=start.data["attempt_id"])
        attempt.started_at = timezone.now() - timedelta(minutes=30)
        attempt.save(update_fields=["started_at"])

        submit = c.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": [{"question": str(self.question.id), "selected_choice": str(self.right.id)}]},
            format="json",
        )
        self.assertEqual(submit.status_code, 400, submit.content)

    def test_resuming_after_the_deadline_discards_the_stale_zero_answer_attempt(self):
        c = self.client_as_student()
        start = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        attempt_id = start.data["attempt_id"]

        stale = QuizAttempt.objects.get(id=attempt_id)
        stale.started_at = timezone.now() - timedelta(minutes=30)
        stale.save(update_fields=["started_at"])

        resumed = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        self.assertNotEqual(resumed.data["attempt_id"], attempt_id)

        # The abandoned 0-answer attempt is DELETED, not left as a
        # SUBMITTED ghost that would falsely mark the quiz completed.
        self.assertFalse(QuizAttempt.objects.filter(id=attempt_id).exists())

    def test_expired_attempt_with_answers_is_closed_out_not_deleted(self):
        # Defensive: if a resumed attempt somehow holds answers when it
        # expires, we preserve it (SUBMITTED) rather than destroying data.
        c = self.client_as_student()
        start = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        attempt = QuizAttempt.objects.get(id=start.data["attempt_id"])
        StudentAnswer.objects.create(
            attempt=attempt, question=self.question,
            selected_choice=self.right, is_correct=True,
        )
        attempt.started_at = timezone.now() - timedelta(minutes=30)
        attempt.save(update_fields=["started_at"])

        resumed = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(resumed.status_code, 200, resumed.content)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, QuizAttempt.STATUS_SUBMITTED)

    def test_zero_answer_submitted_attempt_does_not_mark_quiz_completed(self):
        # Remediation cover for ghost rows already in the DB: a SUBMITTED
        # attempt with no answers must NOT report the quiz as completed on
        # the student dashboard (which is what locked students out).
        QuizAttempt.objects.create(
            quiz=self.quiz, student=self.student, learner_profile=self.profile,
            attempt_number=1, status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=timezone.now(),
        )
        c = self.client_as_student()
        r = c.get("/api/student/quizzes/")
        self.assertEqual(r.status_code, 200, r.content)
        row = next(q for q in r.data if str(q["id"]) == str(self.quiz.id))
        self.assertNotEqual(row["status"], "SUBMITTED")
        self.assertEqual(row["attempts_count"], 0)

    def test_answered_submitted_attempt_still_marks_quiz_completed(self):
        # Guard the invariant in the other direction: a real attempt with
        # answers must still count as completed (no free-retake loophole /
        # no regression for legitimate completions).
        c = self.client_as_student()
        submit = self._start_and_submit(c, self.right)
        self.assertEqual(submit.status_code, 200, submit.content)

        r = c.get("/api/student/quizzes/")
        self.assertEqual(r.status_code, 200, r.content)
        row = next(q for q in r.data if str(q["id"]) == str(self.quiz.id))
        self.assertEqual(row["status"], "SUBMITTED")
        self.assertEqual(row["attempts_count"], 1)


class StudentQuizSubjectsProfileScopingTest(TestCase):
    """Regression cover: this view used to filter subscriptions by account,
    not learner profile, leaking sibling profiles' subject/teacher metadata
    into each other's quiz-subject picker."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")

        cls.account = User.objects.create_user(
            username="sib_acct", email="sib_acct@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="STUDENT"), is_active=True, is_primary=True)

        cls.profile_a = LearnerProfile.objects.create(account=cls.account, display_name="A", is_default=True)
        cls.profile_b = LearnerProfile.objects.create(account=cls.account, display_name="B")

        cls.course_a = Course.objects.create(title="A's Course")
        cls.subject_a = Subject.objects.create(course=cls.course_a, name="A Subject")
        cls.course_b = Course.objects.create(title="B's Course")
        cls.subject_b = Subject.objects.create(course=cls.course_b, name="B Subject")

        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile_a, course=cls.course_a,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile_b, course=cls.course_b,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )

    def test_profile_a_does_not_see_profile_bs_subjects(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile_a.id)},
        )
        r = c.get("/api/student/quiz-subjects/")
        self.assertEqual(r.status_code, 200, r.content)
        names = [row["subject"] for row in r.data]
        self.assertIn("A Subject", names)
        self.assertNotIn("B Subject", names)


class DualRoleStudentQuizAccessTest(TestCase):
    """Regression cover: QuizDetailView used to branch on the account-level
    `user.has_role("TEACHER")` instead of the request's actual context. A
    dual-role account (STUDENT + an active TEACHER role — this platform
    explicitly supports holding several active roles at once) hit the
    teacher-ownership branch even while taking a quiz in a learner-context
    token, 403'ing with "Not authorized for this quiz." QuizDetailView now
    uses accounts.permissions._in_teacher_context(), matching every other
    teacher-gated view in this app."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.other_teacher = User.objects.create_user(
            username="other_teacher", email="other_teacher@test.com", password="x",
        )
        UserRole.objects.create(user=cls.other_teacher, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=True)

        # A dual-role account: active STUDENT role AND an active (but
        # unrelated) TEACHER role. Not the quiz's `created_by`.
        cls.account = User.objects.create_user(
            username="dual_role_q", email="dual_role_q@test.com", password="x",
            is_verified=True,
        )
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="STUDENT"), is_active=True, is_primary=True)
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=False)

        cls.profile = LearnerProfile.objects.create(account=cls.account, display_name="Learner side", is_default=True)

        cls.course = Course.objects.create(title="Bio Demo")
        cls.subject = Subject.objects.create(course=cls.course, name="Biology")
        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )

        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.other_teacher, title="Cell structure",
            quiz_type=Quiz.TYPE_MOCK, is_published=True, review_status=Quiz.REVIEW_APPROVED,
        )

    def test_quiz_detail_accessible_to_dual_role_student_in_learner_context(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        r = c.get(f"/api/quizzes/{self.quiz.id}/")
        self.assertEqual(r.status_code, 200, r.content)
