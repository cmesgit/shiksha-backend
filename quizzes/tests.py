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
from courses.models import Course, Subject, TeachingAssignment
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

    def _start_and_submit(self, client, choice, *, new_attempt=False):
        # new_attempt: a RETAKE over an already-submitted attempt must be
        # asked for explicitly (see StartQuizView's docstring). The attempt
        # route re-posts to this endpoint on every mount, and the browser
        # Back button lands there, so a stray back-click used to silently
        # burn an attempt — which can push a learner past
        # reveal_answers_after and cost them the answer key for good.
        start = client.post(
            f"/api/quizzes/{self.quiz.id}/start/",
            {"new_attempt": True} if new_attempt else {},
            format="json",
        )
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
        submit2 = self._start_and_submit(c, self.right, new_attempt=True)  # attempt 2
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


class QuizCourseScopingTest(TestCase):
    """A learner profile subscribed to several courses must only see the
    ACTIVE course's quizzes.

    The Hub endpoint is flat (/api/student/quizzes/) rather than
    course-scoped by URL the way assignments and materials are, and its
    subscription join spans every course the profile holds. With no ?course=
    filter it therefore returned a Class 7 quiz to a learner sitting in
    Class 12 — reported from production, where the Completed tab showed
    another class's Civics quizzes entirely.

    Also pins batch isolation, which Quiz.batch's own docstring flagged as
    declared-but-unenforced.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(username="cs_t", email="cs_t@test.com", password="x")
        cls.student = User.objects.create_user(
            username="cs_s", email="cs_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="Nil", is_default=True,
        )

        now = timezone.now()
        cls.course_a = Course.objects.create(title="Class 12 (Commerce)")
        cls.course_b = Course.objects.create(title="Class 7")
        cls.subject_a = Subject.objects.create(course=cls.course_a, name="Accountancy")
        cls.subject_b = Subject.objects.create(course=cls.course_b, name="Civics")

        for course in (cls.course_a, cls.course_b):
            Subscription.objects.create(
                user=cls.student, learner_profile=cls.profile, course=course,
                status=Subscription.STATUS_ACTIVE,
                starts_at=now, expires_at=now + timedelta(days=30),
            )

        cls.quiz_a = Quiz.objects.create(
            subject=cls.subject_a, created_by=cls.teacher, title="Ledger basics",
            quiz_type=Quiz.TYPE_MOCK, is_published=True, review_status=Quiz.REVIEW_APPROVED,
        )
        cls.quiz_b = Quiz.objects.create(
            subject=cls.subject_b, created_by=cls.teacher, title="Fundamental rights",
            quiz_type=Quiz.TYPE_MOCK, is_published=True, review_status=Quiz.REVIEW_APPROVED,
        )

    def client_as_student(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _titles(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return {row["title"] for row in response.data}

    def test_course_param_excludes_other_courses_quizzes(self):
        c = self.client_as_student()
        self.assertEqual(
            self._titles(c.get("/api/student/quizzes/", {"course": str(self.course_a.id)})),
            {"Ledger basics"},
        )
        self.assertEqual(
            self._titles(c.get("/api/student/quizzes/", {"course": str(self.course_b.id)})),
            {"Fundamental rights"},
        )

    def test_without_course_param_the_whole_profile_is_returned(self):
        # The un-scoped behaviour is retained deliberately for any caller
        # that wants a cross-course view; the leak was the Hub never passing
        # ?course=, not the parameter being optional.
        c = self.client_as_student()
        self.assertEqual(
            self._titles(c.get("/api/student/quizzes/")),
            {"Ledger basics", "Fundamental rights"},
        )

    def test_batch_scoped_quiz_is_hidden_from_another_batch(self):
        from courses.models import Batch
        from enrollments.models import Enrollment

        batch_mine = Batch.objects.create(course=self.course_a, name="Morning 2026", code="M26")
        batch_other = Batch.objects.create(course=self.course_a, name="Evening 2026", code="E26")
        Enrollment.objects.create(
            user=self.student, learner_profile=self.profile, course=self.course_a,
            batch=batch_mine, status=Enrollment.STATUS_ACTIVE,
        )
        Quiz.objects.create(
            subject=self.subject_a, created_by=self.teacher, title="Evening-only test",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, batch=batch_other,
        )
        mine = Quiz.objects.create(
            subject=self.subject_a, created_by=self.teacher, title="Morning-only test",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, batch=batch_mine,
        )

        c = self.client_as_student()
        titles = self._titles(c.get("/api/student/quizzes/", {"course": str(self.course_a.id)}))
        self.assertIn(mine.title, titles)
        self.assertIn("Ledger basics", titles)          # course-wide (batch NULL)
        self.assertNotIn("Evening-only test", titles)

    def test_stats_are_scoped_to_the_course(self):
        # Answering a Class 7 question must not move the Class 12 strip.
        question = Question.objects.create(quiz=self.quiz_b, text="Art. 21?", marks=1, order=0)
        choice = Choice.objects.create(question=question, text="Life", is_correct=True)
        attempt = QuizAttempt.objects.create(
            quiz=self.quiz_b, learner_profile=self.profile, student=self.student,
            status=QuizAttempt.STATUS_SUBMITTED, attempt_number=1, score=1,
        )
        StudentAnswer.objects.create(
            attempt=attempt, question=question, selected_choice=choice, is_correct=True,
        )

        c = self.client_as_student()
        a = c.get("/api/student/quizzes/stats/", {"course": str(self.course_a.id)})
        b = c.get("/api/student/quizzes/stats/", {"course": str(self.course_b.id)})
        self.assertEqual(a.status_code, 200, a.content)
        self.assertEqual(b.status_code, 200, b.content)
        self.assertEqual(a.data["questions_solved"], 0)
        self.assertEqual(b.data["questions_solved"], 1)


class QuizAccidentalRetakeTest(TestCase):
    """Landing on the attempt route again must not silently start a retake.

    QuizMock re-posts /start/ on every mount and the browser Back button
    lands there, so a stray back-click from the result screen used to create
    a whole new attempt: reshuffled questions, a running timer, and an
    inflated attempt_number that can push the learner past
    reveal_answers_after and permanently cost them the answer key.
    Retakes stay unlimited — they just have to be asked for.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(username="ar_t", email="ar_t@test.com", password="x")
        cls.student = User.objects.create_user(
            username="ar_s", email="ar_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="S", is_default=True,
        )
        cls.course = Course.objects.create(title="Bio")
        cls.subject = Subject.objects.create(course=cls.course, name="Biology")
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Cells",
            quiz_type=Quiz.TYPE_MOCK, is_published=True, review_status=Quiz.REVIEW_APPROVED,
        )
        cls.question = Question.objects.create(quiz=cls.quiz, text="Powerhouse?", marks=1, order=0)
        cls.right = Choice.objects.create(question=cls.question, text="Mitochondria", is_correct=True)

    def _client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _submit(self, c):
        return c.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": [{"question": str(self.question.id), "selected_choice": str(self.right.id)}]},
            format="json",
        )

    def test_first_start_still_creates_an_attempt_with_no_flag(self):
        c = self._client()
        r = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.data.get("already_submitted", False))
        self.assertEqual(QuizAttempt.objects.filter(quiz=self.quiz).count(), 1)

    def test_reposting_start_after_submitting_creates_nothing(self):
        c = self._client()
        c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(self._submit(c).status_code, 200)

        again = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(again.status_code, 200, again.content)
        self.assertTrue(again.data["already_submitted"])
        # Still exactly one attempt — the back-click cost the learner nothing.
        self.assertEqual(QuizAttempt.objects.filter(quiz=self.quiz).count(), 1)

    def test_explicit_new_attempt_still_starts_a_retake(self):
        c = self._client()
        c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self._submit(c)

        retake = c.post(
            f"/api/quizzes/{self.quiz.id}/start/", {"new_attempt": True}, format="json",
        )
        self.assertEqual(retake.status_code, 200, retake.content)
        self.assertFalse(retake.data.get("already_submitted", False))
        self.assertEqual(QuizAttempt.objects.filter(quiz=self.quiz).count(), 2)

    def test_an_in_progress_attempt_is_still_resumed_not_duplicated(self):
        # Refreshing mid-attempt must keep the same attempt (and its clock).
        c = self._client()
        first = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        resumed = c.post(f"/api/quizzes/{self.quiz.id}/start/")
        self.assertEqual(resumed.data["attempt_id"], first.data["attempt_id"])
        self.assertEqual(QuizAttempt.objects.filter(quiz=self.quiz).count(), 1)


class BulkQuestionReplaceSemanticsTest(TestCase):
    """PUT .../questions/bulk/ is REPLACE, not merge — omitting an existing
    question's id DELETES it, with its choices and its answer key.

    That contract is deliberate (it is how the builder reorders and removes
    questions in one round trip), but it is also a loaded gun: the builder
    used to send only its *complete* questions, so clearing Q2's explanation
    to retype it and hitting "Save draft" silently destroyed Q2 for good
    while the UI said "Draft saved". The client now refuses to save while any
    question is incomplete (QuizBuilder.jsx's `persist`), rather than
    dropping it from the payload. These tests pin the server contract that
    fix depends on, so nobody "helpfully" makes the PUT merge instead.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="bulk_t", email="bulk_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.course = Course.objects.create(title="Phys")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Motion",
            quiz_type=Quiz.TYPE_MOCK, review_status=Quiz.REVIEW_DRAFT,
        )

    def setUp(self):
        self.q1 = Question.objects.create(
            quiz=self.quiz, text="Unit of force?", marks=2, order=0,
            explanation="SI base units.",
        )
        Choice.objects.create(question=self.q1, text="Newton", is_correct=True)
        Choice.objects.create(question=self.q1, text="Joule", is_correct=False)

        self.q2 = Question.objects.create(
            quiz=self.quiz, text="Unit of power?", marks=3, order=1,
            explanation="Work per unit time.",
        )
        Choice.objects.create(question=self.q2, text="Watt", is_correct=True)
        Choice.objects.create(question=self.q2, text="Pascal", is_correct=False)

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def _payload_for(self, question):
        return {
            "id": str(question.id),
            "text": question.text,
            "marks": question.marks,
            "order": question.order,
            "explanation": question.explanation,
            "choices": [
                {"text": c.text, "is_correct": c.is_correct}
                for c in question.choices.all()
            ],
        }

    def test_omitting_an_existing_question_deletes_it_and_its_choices(self):
        # This is the destructive contract the builder must never trip over.
        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(Question.objects.filter(id=self.q2.id).exists())
        self.assertFalse(Choice.objects.filter(question_id=self.q2.id).exists())
        self.assertTrue(Question.objects.filter(id=self.q1.id).exists())

    def test_sending_both_questions_preserves_both(self):
        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1), self._payload_for(self.q2)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.quiz.questions.count(), 2)
        self.q2.refresh_from_db()
        self.assertEqual(self.q2.marks, 3)
        self.assertEqual(self.q2.choices.count(), 2)

    def test_a_question_missing_its_explanation_is_rejected_wholesale(self):
        # All-or-nothing: the rejected payload must not have deleted anything
        # on its way out. This is what makes the client's "block the save"
        # fix safe — a partial save can never half-apply.
        c = self._client()
        bad = self._payload_for(self.q2)
        bad["explanation"] = ""
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1), bad]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self.quiz.questions.count(), 2)
        self.assertTrue(Question.objects.filter(id=self.q2.id).exists())


class QuizResultTotalsTest(TestCase):
    """The result screen's denominator is the PAPER, not what was answered.

    QuizResultView builds `questions` from attempt.answers, so a partial
    attempt returned a short list. The client divided correct-by-that-length
    and rendered 100% accuracy over "5 / 20 marks"; a zero-answer attempt
    rendered NaN. `questions_total` is the honest denominator.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(username="rt_t", email="rt_t@test.com", password="x")
        cls.student = User.objects.create_user(
            username="rt_s", email="rt_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="R", is_default=True,
        )
        cls.course = Course.objects.create(title="Hist")
        cls.subject = Subject.objects.create(course=cls.course, name="History")
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Mughals",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, total_marks=4,
        )
        cls.questions = []
        for i in range(4):
            q = Question.objects.create(quiz=cls.quiz, text=f"Q{i}", marks=1, order=i)
            Choice.objects.create(question=q, text="right", is_correct=True)
            Choice.objects.create(question=q, text="wrong", is_correct=False)
            cls.questions.append(q)

    def _client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def test_partial_attempt_reports_the_full_paper_size(self):
        c = self._client()
        c.post(f"/api/quizzes/{self.quiz.id}/start/")
        first = self.questions[0]
        c.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": [{
                "question": str(first.id),
                "selected_choice": str(first.choices.get(is_correct=True).id),
            }]},
            format="json",
        )

        r = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        self.assertEqual(r.status_code, 200, r.content)
        # One answered, one correct — but the paper has four questions, and
        # 1/1 == 100% is the wrong story to tell above "1 / 4 marks".
        self.assertEqual(len(r.data["questions"]), 1)
        self.assertEqual(r.data["questions_total"], 4)
        self.assertEqual(r.data["score"], 1)

    def test_zero_answer_attempt_still_reports_a_usable_denominator(self):
        c = self._client()
        c.post(f"/api/quizzes/{self.quiz.id}/start/")
        c.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": []}, format="json",
        )

        r = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["questions"], [])
        # Nonzero, so the client divides by 4 and renders 0% — not NaN%.
        self.assertEqual(r.data["questions_total"], 4)


class TeacherQuizRosterProfileSplitTest(TestCase):
    """Two siblings on one parent account are two rows, not one (theme T2).

    The roster grouped on student_id (the ACCOUNT) and named the row from the
    account's DEFAULT profile, so Riya (default, 1/2) and her brother Arjun
    (2/2) merged into "Riya · 2 attempts · best 2/2".
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="ros_t", email="ros_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.parent = User.objects.create_user(
            username="ros_p", email="ros_p@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.parent, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.riya = LearnerProfile.objects.create(
            account=cls.parent, display_name="Riya", full_name="Riya Sharma",
            is_default=True,
        )
        cls.arjun = LearnerProfile.objects.create(
            account=cls.parent, display_name="Arjun", full_name="Arjun Sharma",
            is_default=False,
        )

        cls.course = Course.objects.create(title="Geo")
        cls.subject = Subject.objects.create(course=cls.course, name="Geography")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Rivers",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, total_marks=2,
        )
        cls.question = Question.objects.create(
            quiz=cls.quiz, text="Longest river?", marks=2, order=0,
            explanation="Nile.",
        )
        cls.right = Choice.objects.create(question=cls.question, text="Nile", is_correct=True)
        cls.wrong = Choice.objects.create(question=cls.question, text="Thames", is_correct=False)

        now = timezone.now()
        cls.riya_attempt = QuizAttempt.objects.create(
            quiz=cls.quiz, student=cls.parent, learner_profile=cls.riya,
            attempt_number=1, score=0, status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=now,
        )
        StudentAnswer.objects.create(
            attempt=cls.riya_attempt, question=cls.question,
            selected_choice=cls.wrong, is_correct=False,
        )
        cls.arjun_attempt = QuizAttempt.objects.create(
            quiz=cls.quiz, student=cls.parent, learner_profile=cls.arjun,
            attempt_number=1, score=2, status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=now,
        )
        StudentAnswer.objects.create(
            attempt=cls.arjun_attempt, question=cls.question,
            selected_choice=cls.right, is_correct=True,
        )

    def _teacher_client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def test_siblings_get_their_own_roster_rows(self):
        r = self._teacher_client().get(f"/api/teacher/quizzes/{self.quiz.id}/attempts/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 2)

        by_name = {row["student_name"]: row for row in r.data}
        self.assertEqual(set(by_name), {"Riya Sharma", "Arjun Sharma"})
        # Each child's own score, not the account's best smeared over both.
        self.assertEqual(by_name["Riya Sharma"]["best_score"], 0)
        self.assertEqual(by_name["Arjun Sharma"]["best_score"], 2)
        self.assertEqual(by_name["Riya Sharma"]["attempts_count"], 1)
        self.assertEqual(by_name["Arjun Sharma"]["attempts_count"], 1)

    def test_the_row_drill_down_returns_only_that_childs_attempts(self):
        c = self._teacher_client()
        roster = c.get(f"/api/teacher/quizzes/{self.quiz.id}/attempts/").data
        arjun_row = next(r for r in roster if r["student_name"] == "Arjun Sharma")

        r = c.get(f"/api/teacher/quizzes/{self.quiz.id}/attempts/{arjun_row['student_id']}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(str(r.data[0]["id"]), str(self.arjun_attempt.id))

    def test_a_legacy_profileless_attempt_still_drills_down_on_the_account_id(self):
        legacy = QuizAttempt.objects.create(
            quiz=self.quiz, student=self.teacher, learner_profile=None,
            attempt_number=1, score=1, status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=timezone.now(),
        )
        c = self._teacher_client()
        r = c.get(f"/api/teacher/quizzes/{self.quiz.id}/attempts/{self.teacher.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([str(row["id"]) for row in r.data], [str(legacy.id)])


class TeacherAttemptDetailContextGateTest(TestCase):
    """TeacherQuizAttemptDetailView was the only teacher endpoint in this app
    with no IsTeacherContext. teaches_subject() alone passes for a teacher's
    account even when the token is in LEARNER context — i.e. when the
    teacher's own child is using the browser — exposing any classmate's full
    name, score and answers with no teacher-password gate."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="ctx_t", email="ctx_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.teacher_child = LearnerProfile.objects.create(
            account=cls.teacher, display_name="Teacher's kid", is_default=True,
        )

        cls.classmate = User.objects.create_user(
            username="ctx_s", email="ctx_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.classmate, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.classmate_profile = LearnerProfile.objects.create(
            account=cls.classmate, display_name="Classmate",
            full_name="Priya Verma", is_default=True,
        )

        cls.course = Course.objects.create(title="Maths")
        cls.subject = Subject.objects.create(course=cls.course, name="Algebra")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Linear equations",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, total_marks=2,
        )
        cls.q1 = Question.objects.create(
            quiz=cls.quiz, text="Solve 2x=4", marks=1, order=0, explanation="x=2",
        )
        cls.q1_right = Choice.objects.create(question=cls.q1, text="2", is_correct=True)
        Choice.objects.create(question=cls.q1, text="4", is_correct=False)
        # Deliberately never answered — it must still appear in the review.
        cls.q2 = Question.objects.create(
            quiz=cls.quiz, text="Solve 3x=9", marks=1, order=1, explanation="x=3",
        )
        Choice.objects.create(question=cls.q2, text="3", is_correct=True)
        Choice.objects.create(question=cls.q2, text="6", is_correct=False)

        cls.attempt = QuizAttempt.objects.create(
            quiz=cls.quiz, student=cls.classmate,
            learner_profile=cls.classmate_profile, attempt_number=1, score=1,
            status=QuizAttempt.STATUS_SUBMITTED, submitted_at=timezone.now(),
        )
        StudentAnswer.objects.create(
            attempt=cls.attempt, question=cls.q1,
            selected_choice=cls.q1_right, is_correct=True,
        )

    def test_learner_context_on_a_teacher_account_is_refused(self):
        c = APIClient()
        c.force_authenticate(
            user=self.teacher,
            token={"context": "learner", "active_profile": str(self.teacher_child.id)},
        )
        r = c.get(f"/api/teacher/attempts/{self.attempt.id}/")
        self.assertEqual(r.status_code, 403, r.content)
        # And nothing about the classmate leaked in the error body.
        self.assertNotIn("Priya", str(r.content))

    def test_teacher_context_still_reads_the_attempt(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get(f"/api/teacher/attempts/{self.attempt.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["student_name"], "Priya Verma")

    def test_the_review_lists_skipped_questions_too(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get(f"/api/teacher/attempts/{self.attempt.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        # 1 of 2 answered — the review used to render this as a 1-question quiz.
        self.assertEqual(len(r.data["questions"]), 2)
        answered, skipped = r.data["questions"]
        self.assertTrue(answered["answered"])
        self.assertTrue(answered["is_correct"])
        self.assertFalse(skipped["answered"])
        self.assertIsNone(skipped["selected"])
        self.assertFalse(skipped["is_correct"])
        # The key is still shown for the skipped one, so the teacher can mark it.
        self.assertEqual(skipped["correct"], "3")


class TeacherQuizCardStatsTest(TestCase):
    """Quiz-card aggregates must count SUBMITTED attempts only, and must rate
    submission by distinct LEARNERS.

    A PENDING row (opened the quiz, closed the tab) carries score=0, so it
    dragged average_score down and inflated total_attempts — and the card
    then disagreed with its own analytics screen, which has always filtered
    on SUBMITTED. submission_rate counted attempts, so one student retaking
    read as several students submitting.
    """

    @classmethod
    def setUpTestData(cls):
        from enrollments.models import Enrollment

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="cs_t", email="cs_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.student = User.objects.create_user(
            username="cs_s", email="cs_s@test.com", password="x", is_verified=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="One", is_default=True,
        )

        cls.course = Course.objects.create(title="Eng")
        cls.subject = Subject.objects.create(course=cls.course, name="English")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        Enrollment.objects.create(
            user=cls.student, learner_profile=cls.profile,
            course=cls.course, status="ACTIVE",
        )
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Poetry",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, total_marks=10,
        )

        # Two real submissions by the SAME learner, plus one abandoned tab.
        for n, score in ((1, 8.0), (2, 7.0)):
            QuizAttempt.objects.create(
                quiz=cls.quiz, student=cls.student, learner_profile=cls.profile,
                attempt_number=n, score=score,
                status=QuizAttempt.STATUS_SUBMITTED, submitted_at=timezone.now(),
            )
        QuizAttempt.objects.create(
            quiz=cls.quiz, student=cls.student, learner_profile=cls.profile,
            attempt_number=3, score=0, status=QuizAttempt.STATUS_PENDING,
        )

    def _row(self, url):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get(url)
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_flat_list_ignores_the_pending_attempt(self):
        row = self._row("/api/teacher/quizzes/all/")
        self.assertEqual(row["total_attempts"], 2)
        # (8 + 7) / 2 — the pending 0 would have made this 5.0.
        self.assertEqual(row["average_score"], 7.5)
        self.assertEqual(row["lowest_score"], 7.0)
        # One learner out of one enrolled, despite three attempt rows.
        self.assertEqual(row["submission_rate"], 100.0)
        # The denominator the card needs to render 7.5 as a percentage.
        self.assertEqual(row["total_marks"], 10)

    def test_per_subject_list_agrees_with_the_flat_list(self):
        row = self._row(f"/api/teacher/subjects/{self.subject.id}/quizzes/")
        self.assertEqual(row["total_attempts"], 2)
        self.assertEqual(row["average_score"], 7.5)
        self.assertEqual(row["submission_rate"], 100.0)


class TeacherQuizDetailAnswerKeyTest(TestCase):
    """The owning teacher's "View quiz" must include the answer key.

    /api/quizzes/<pk>/ served QuestionPublicSerializer to everyone, which
    strips is_correct and the explanation by design — so the teacher's own
    view of a quiz they wrote could never highlight the correct option.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="ak_t", email="ak_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.other = User.objects.create_user(
            username="ak_t2", email="ak_t2@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.other, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.course = Course.objects.create(title="Civics")
        cls.subject = Subject.objects.create(course=cls.course, name="Polity")
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Parliament",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED,
        )
        cls.question = Question.objects.create(
            quiz=cls.quiz, text="Upper house?", marks=1, order=0,
            explanation="Rajya Sabha is the council of states.",
        )
        Choice.objects.create(question=cls.question, text="Rajya Sabha", is_correct=True)
        Choice.objects.create(question=cls.question, text="Lok Sabha", is_correct=False)

    def test_owning_teacher_sees_is_correct_and_the_explanation(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get(f"/api/quizzes/{self.quiz.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        question = r.data["questions"][0]
        self.assertIn("Rajya Sabha", question["explanation"])
        correct = [c["text"] for c in question["choices"] if c["is_correct"]]
        self.assertEqual(correct, ["Rajya Sabha"])

    def test_a_different_teacher_is_still_refused(self):
        c = APIClient()
        c.force_authenticate(user=self.other, token={"context": "teacher"})
        r = c.get(f"/api/quizzes/{self.quiz.id}/")
        self.assertEqual(r.status_code, 403, r.content)


class QuizStudentEndpointBatchIsolationTest(TestCase):
    """StartQuizView/QuizDetailView/SubmitQuizView used to resolve a quiz by
    UUID with only is_published + subscription checks — no batch check, even
    though QuizCourseScopingTest already proved the LIST endpoint enforces
    batch isolation (only when ?course= is passed). A Batch-B student who
    has, or guesses, a Batch-A-only quiz's UUID could start/view/submit it.
    Now gated by _assert_learner_may_see_quiz(), mirroring
    assignments._assert_learner_may_see_assignment."""

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment

        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(username="bi_t", email="bi_t@test.com", password="x")
        cls.student = User.objects.create_user(
            username="bi_s", email="bi_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="Nil", is_default=True,
        )

        now = timezone.now()
        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )

        cls.batch_mine = Batch.objects.create(course=cls.course, name="10-A", code="10A")
        cls.batch_other = Batch.objects.create(course=cls.course, name="10-B", code="10B")
        Enrollment.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            batch=cls.batch_mine, status=Enrollment.STATUS_ACTIVE,
        )

        cls.quiz_mine = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="10-A test",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, batch=cls.batch_mine,
        )
        cls.quiz_other = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="10-B test",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, batch=cls.batch_other,
        )
        cls.quiz_course_wide = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Evergreen test",
            quiz_type=Quiz.TYPE_MOCK, is_published=True,
            review_status=Quiz.REVIEW_APPROVED, batch=None,
        )

    def client_as_student(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def test_cannot_start_another_batchs_quiz(self):
        r = self.client_as_student().post(f"/api/quizzes/{self.quiz_other.id}/start/")
        self.assertEqual(r.status_code, 404, r.content)

    def test_can_start_own_batchs_quiz(self):
        r = self.client_as_student().post(f"/api/quizzes/{self.quiz_mine.id}/start/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_can_start_course_wide_quiz(self):
        r = self.client_as_student().post(f"/api/quizzes/{self.quiz_course_wide.id}/start/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_cannot_view_another_batchs_quiz_detail(self):
        r = self.client_as_student().get(f"/api/quizzes/{self.quiz_other.id}/")
        self.assertEqual(r.status_code, 404, r.content)

    def test_can_view_own_batchs_quiz_detail(self):
        r = self.client_as_student().get(f"/api/quizzes/{self.quiz_mine.id}/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_cannot_submit_another_batchs_quiz(self):
        r = self.client_as_student().post(
            f"/api/student/quizzes/{self.quiz_other.id}/submit/",
            {"answers": []}, format="json",
        )
        self.assertEqual(r.status_code, 404, r.content)


class QuizCreateBatchAndChapterTest(TestCase):
    """CreateQuizView used to accept no batch/chapter at all — Quiz.batch
    was declared but never set by any code path. Now mirrors Assignments:
    batch is required and authz-checked via is_teacher_of(), and the
    chapter is either an existing one or a brand-new custom_chapter name
    resolved via courses.services.resolve_or_create_chapter()."""

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch

        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="cq_t", email="cq_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )

        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.batch = Batch.objects.create(course=cls.course, name="9-A", code="9A")
        TeachingAssignment.objects.create(
            batch=cls.batch, subject=cls.subject, teacher=cls.teacher, is_active=True,
        )

    def _teacher_client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def test_batch_is_required(self):
        r = self._teacher_client().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "title": "No batch",
            "custom_chapter": "Motion",
        })
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("batch_id", r.data)

    def test_custom_chapter_creates_a_real_chapter(self):
        from courses.models import Chapter

        r = self._teacher_client().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "batch_id": str(self.batch.id),
            "title": "Motion quiz", "custom_chapter": "Laws of Motion",
        })
        self.assertEqual(r.status_code, 201, r.content)
        chapter = Chapter.objects.get(subject=self.subject, title="Laws of Motion")
        quiz = Quiz.objects.get(id=r.data["id"])
        self.assertEqual(quiz.chapter_id, chapter.id)
        self.assertEqual(quiz.batch_id, self.batch.id)

    def test_batch_in_a_batch_the_teacher_does_not_teach_is_rejected(self):
        from courses.models import Batch

        other_batch = Batch.objects.create(course=self.course, name="9-B", code="9B")
        r = self._teacher_client().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "batch_id": str(other_batch.id),
            "title": "Not mine", "custom_chapter": "Motion",
        })
        self.assertEqual(r.status_code, 400, r.content)

    def test_editing_title_does_not_require_batch_or_chapter(self):
        from courses.models import Chapter

        chapter = Chapter.objects.create(subject=self.subject, title="Optics")
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher, title="Original",
            quiz_type=Quiz.TYPE_MOCK, batch=self.batch, chapter=chapter,
        )
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/", {"title": "Renamed"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        quiz.refresh_from_db()
        self.assertEqual(quiz.title, "Renamed")
        self.assertEqual(quiz.batch_id, self.batch.id)
        self.assertEqual(quiz.chapter_id, chapter.id)
