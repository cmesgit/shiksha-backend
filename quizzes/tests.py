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
from decimal import Decimal

from django.db.models import Q
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.chapter_tags import set_tags
from courses.models import Course, Subject, TeachingAssignment
from enrollments.models import Subscription
from quizzes.models import (
    Quiz, QuizSection, Question, Choice, QuizAttempt, StudentAnswer,
)


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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, review_status=Quiz.REVIEW_APPROVED,
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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, review_status=Quiz.REVIEW_APPROVED,
        )
        cls.quiz_b = Quiz.objects.create(
            subject=cls.subject_b, created_by=cls.teacher, title="Fundamental rights",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, review_status=Quiz.REVIEW_APPROVED,
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
        # The cross-COURSE behaviour is retained deliberately for any caller
        # that wants a whole-profile view; the leak was the Hub never passing
        # ?course=, not the parameter being optional.
        #
        # Note this says nothing about BATCH scoping, which now applies on
        # this call too — both quizzes here are course-wide (empty `batches`)
        # so they reach every batch either way. QuizNoCourseParamBatchScoping-
        # Test covers the batch half, which used to be skipped entirely when
        # no ?course= was passed.
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
        evening = Quiz.objects.create(
            subject=self.subject_a, created_by=self.teacher, title="Evening-only test",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED,
        )
        # Scope lives in the M2M — the `batch` shim was dropped in Phase 10,
        # and an empty batches set means "every batch of the course", which
        # would make these isolation assertions vacuously pass.
        evening.batches.set([batch_other])
        mine = Quiz.objects.create(
            subject=self.subject_a, created_by=self.teacher, title="Morning-only test",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED,
        )
        mine.batches.set([batch_mine])

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


class QuizNoCourseParamBatchScopingTest(TestCase):
    """The Hub list must scope by batch even when no ?course= is passed.

    Sibling of QuizCourseScopingTest, which pins the *course* half of the
    rule. This pins the *batch* half on the un-scoped call, which used to
    skip it entirely: the batch filter in StudentDashboardView lived inside
    `if course_id:`, so `GET /api/student/quizzes/` returned every batch's
    quizzes for every course the profile held a subscription to.

    Reproduced against a running server before it was fixed — 5 rows
    including another batch's quiz with no params, 0 rows (correct) with
    `?course=`. Information disclosure only: the per-id endpoints
    (`/quizzes/<id>/`, `/quizzes/<id>/start/`) go through
    `learner_may_see_quiz` and correctly 404, and QuizHub.jsx always sends
    `?course=`, so nothing shipped could reach it. Fixed anyway — the
    endpoint is public API surface and the next caller need not pass it.

    TRAP, and it cost real time during verification: these endpoints gate on
    an ACTIVE Subscription, so a fixture student who is merely *enrolled*
    gets an empty list and every batch-isolation assertion below passes
    vacuously. Both learners here get an Enrollment AND a live Subscription,
    and `test_the_fixture_actually_sees_quizzes` asserts the list is
    non-empty so this class can never rot back into passing for that reason.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment

        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(
            username="nb_t", email="nb_t@test.com", password="x",
        )

        now = timezone.now()
        cls.course = Course.objects.create(title="Class 11 (Science)")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.batch_a = Batch.objects.create(course=cls.course, name="Morning", code="NB-A")
        cls.batch_b = Batch.objects.create(course=cls.course, name="Evening", code="NB-B")

        def learner(tag, batch):
            user = User.objects.create_user(
                username=f"nb_{tag}", email=f"nb_{tag}@test.com",
                password="x", is_verified=True,
            )
            UserRole.objects.create(
                user=user, role=Role.objects.get(name="STUDENT"),
                is_active=True, is_primary=True,
            )
            profile = LearnerProfile.objects.create(
                account=user, display_name=tag.upper(), is_default=True,
            )
            Enrollment.objects.create(
                user=user, learner_profile=profile, course=cls.course,
                batch=batch, status=Enrollment.STATUS_ACTIVE,
            )
            # Without this the endpoint returns nothing at all and every
            # assertion below would pass for the wrong reason. See the
            # class docstring.
            Subscription.objects.create(
                user=user, learner_profile=profile, course=cls.course,
                status=Subscription.STATUS_ACTIVE,
                starts_at=now, expires_at=now + timedelta(days=30),
            )
            return user, profile

        cls.user_a, cls.profile_a = learner("a", cls.batch_a)
        cls.user_b, cls.profile_b = learner("b", cls.batch_b)

        def quiz(title, batches):
            q = Quiz.objects.create(
                subject=cls.subject, created_by=cls.teacher, title=title,
                quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
                review_status=Quiz.REVIEW_APPROVED,
            )
            # Scope lives in the M2M — the legacy `batch` FK was dropped in
            # Phase 10, and an EMPTY batches set means "every batch of the
            # course", which would make isolation assertions vacuous.
            if batches:
                q.batches.set(batches)
            return q

        cls.quiz_a_only = quiz("Batch A only", [cls.batch_a])
        cls.quiz_b_only = quiz("Batch B only", [cls.batch_b])
        cls.quiz_shared = quiz("Course-wide", [])

    def _client(self, user, profile):
        c = APIClient()
        c.force_authenticate(
            user=user,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        return c

    def _titles(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return {row["title"] for row in response.data}

    def test_the_fixture_actually_sees_quizzes(self):
        """Guards every other assertion here against the subscription trap."""
        self.assertTrue(self._titles(self._client(self.user_a, self.profile_a)
                                     .get("/api/student/quizzes/")))

    def test_no_course_param_does_not_leak_another_batchs_quiz(self):
        self.assertEqual(
            self._titles(self._client(self.user_a, self.profile_a)
                         .get("/api/student/quizzes/")),
            {"Batch A only", "Course-wide"},
        )
        self.assertEqual(
            self._titles(self._client(self.user_b, self.profile_b)
                         .get("/api/student/quizzes/")),
            {"Batch B only", "Course-wide"},
        )

    def test_the_param_and_no_param_calls_agree(self):
        """The two code paths must not disagree — that gap WAS the bug."""
        for user, profile in ((self.user_a, self.profile_a),
                              (self.user_b, self.profile_b)):
            c = self._client(user, profile)
            self.assertEqual(
                self._titles(c.get("/api/student/quizzes/")),
                self._titles(c.get("/api/student/quizzes/",
                                   {"course": str(self.course.id)})),
            )

    def test_a_learner_unplaced_in_a_batch_sees_course_wide_only(self):
        """batch_id=None degrades to course-wide-only, on both paths.

        Deliberately STRICTER than dashboard/views.py, which widens an
        unplaced learner to every batch in the course. That asymmetry is
        pre-existing and documented on batch_scope_q; what matters here is
        that this endpoint answers the same way with and without ?course=.
        """
        from enrollments.models import Enrollment

        Enrollment.objects.filter(learner_profile=self.profile_a).update(batch=None)
        c = self._client(self.user_a, self.profile_a)
        self.assertEqual(self._titles(c.get("/api/student/quizzes/")), {"Course-wide"})
        self.assertEqual(
            self._titles(c.get("/api/student/quizzes/", {"course": str(self.course.id)})),
            {"Course-wide"},
        )

    def test_a_subscriber_with_no_enrollment_row_sees_course_wide_only(self):
        """Fails closed. active_batch_id returns None for a missing
        enrollment exactly as it does for an unplaced one, and the
        multi-course path must not accidentally widen that to everything by
        having no entry in its {course: batch} map."""
        from enrollments.models import Enrollment

        Enrollment.objects.filter(learner_profile=self.profile_a).delete()
        self.assertEqual(
            self._titles(self._client(self.user_a, self.profile_a)
                         .get("/api/student/quizzes/")),
            {"Course-wide"},
        )

    def test_a_learner_in_different_batches_of_two_courses(self):
        """Why `?course=` could not simply be made required, and why one
        active_batch_id() call is not enough: with no course in scope there
        is no single batch to resolve, so the rule has to be built per
        (course, batch) pair from the learner's active enrollments."""
        from courses.models import Batch
        from enrollments.models import Enrollment

        now = timezone.now()
        course2 = Course.objects.create(title="Class 11 (Commerce)")
        subject2 = Subject.objects.create(course=course2, name="Accountancy")
        c2_mine = Batch.objects.create(course=course2, name="Weekend", code="NB-C")
        c2_other = Batch.objects.create(course=course2, name="Weekday", code="NB-D")
        Enrollment.objects.create(
            user=self.user_a, learner_profile=self.profile_a, course=course2,
            batch=c2_mine, status=Enrollment.STATUS_ACTIVE,
        )
        Subscription.objects.create(
            user=self.user_a, learner_profile=self.profile_a, course=course2,
            status=Subscription.STATUS_ACTIVE,
            starts_at=now, expires_at=now + timedelta(days=30),
        )
        for title, batch in (("C2 mine", c2_mine), ("C2 theirs", c2_other)):
            q = Quiz.objects.create(
                subject=subject2, created_by=self.teacher, title=title,
                quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
                review_status=Quiz.REVIEW_APPROVED,
            )
            q.batches.set([batch])

        # Batch A in course 1, Weekend in course 2 — the pairs must not be
        # crossed (course 1's batch must not unlock course 2's content).
        self.assertEqual(
            self._titles(self._client(self.user_a, self.profile_a)
                         .get("/api/student/quizzes/")),
            {"Batch A only", "Course-wide", "C2 mine"},
        )

    def test_questions_count_is_not_inflated_by_the_multi_course_rule(self):
        """The OR-one-term-per-course shape is exactly what duplicates rows
        under a join-based rule (see
        test_the_batch_rule_adds_no_join_so_needs_no_distinct). Exists() is
        immune; this asserts it at the endpoint, where the annotated
        questions_count would otherwise be multiplied."""
        self.quiz_a_only.batches.set([self.batch_a, self.batch_b])
        for i in range(3):
            Question.objects.create(
                quiz=self.quiz_a_only, text=f"q{i}", marks=1, order=i,
            )
        r = self._client(self.user_a, self.profile_a).get("/api/student/quizzes/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = [x for x in r.data if x["title"] == "Batch A only"]
        self.assertEqual(len(rows), 1, r.data)
        self.assertEqual(rows[0]["questions_count"], 3)


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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, review_status=Quiz.REVIEW_APPROVED,
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

    def test_saving_questions_keeps_total_marks_in_sync(self):
        """`total_marks` is the denominator of every score percentage.

        It used to be recomputed in exactly one place — SubmitQuizForReviewView.
        Phase 1 replaced that route with assign/ and Phase 5b pointed the
        builder at it, so nothing recomputed the field any more and every quiz
        made through the new flow kept the model default of 0. Six different
        score calculations divide by it and each guards against zero by
        bailing out, so the symptom was a silently blank percentage rather than
        an error — a student's best score just stopped being reported.
        """
        self.quiz.total_marks = 0
        self.quiz.save(update_fields=["total_marks"])

        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1), self._payload_for(self.q2)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)

        self.quiz.refresh_from_db()
        # q1 is 2 marks, q2 is 3 (see setUp)
        self.assertEqual(self.quiz.total_marks, 5)

    def test_deleting_a_question_lowers_total_marks(self):
        # The replace contract deletes omitted questions, so the total has to
        # come down too — otherwise every percentage is quietly deflated.
        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.total_marks, 2)

    def test_assigning_resyncs_total_marks_as_a_backstop(self):
        # Whatever route a quiz took, it must not go live with a zero
        # denominator — that is what blanks the student-facing percentages.
        self.quiz.total_marks = 0
        self.quiz.save(update_fields=["total_marks"])

        c = self._client()
        r = c.patch(
            f"/api/teacher/quizzes/{self.quiz.id}/assign/",
            {"assign": True}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.total_marks, 5)
        self.assertTrue(self.quiz.is_assigned)

    def test_turning_suggest_to_bank_off_makes_the_question_private(self):
        """The Phase 2 invariant, through the endpoint that used to skip it.

        This PUT updated existing rows with `queryset.update()`, which goes
        straight to SQL and never runs Question.save() — so the
        `suggest_to_bank=False ⟹ bank_state="private"` rule was bypassed on
        every builder save. It stayed harmless only because the endpoint
        ignored suggest_to_bank entirely. Phase 5d sends it, so without the
        switch to setattr+save() a teacher marking a question "keep this to
        my class" would leave it sitting in the admin's curation queue.
        """
        self.assertEqual(self.q1.bank_state, Question.BANK_STATE_SUGGESTED)

        payload = self._payload_for(self.q1)
        payload["suggest_to_bank"] = False
        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [payload, self._payload_for(self.q2)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)

        self.q1.refresh_from_db()
        self.assertFalse(self.q1.suggest_to_bank)
        self.assertEqual(self.q1.bank_state, Question.BANK_STATE_PRIVATE)

    def test_an_admin_accepted_question_is_not_demoted_by_a_plain_resave(self):
        # The other half of the invariant: re-saving with suggest_to_bank
        # still true must not revert an admin's curation to "suggested".
        self.q1.bank_state = Question.BANK_STATE_ACCEPTED
        self.q1.save()

        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1), self._payload_for(self.q2)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.q1.refresh_from_db()
        self.assertEqual(self.q1.bank_state, Question.BANK_STATE_ACCEPTED)

    def test_omitting_suggest_to_bank_leaves_it_alone(self):
        # A client that never sends the key (the pre-5d builder) must not have
        # its questions re-flagged — same present-vs-absent rule as `section`.
        self.q1.suggest_to_bank = False
        self.q1.save()
        self.assertEqual(self.q1.bank_state, Question.BANK_STATE_PRIVATE)

        c = self._client()
        r = c.put(
            f"/api/teacher/quizzes/{self.quiz.id}/questions/bulk/",
            {"questions": [self._payload_for(self.q1), self._payload_for(self.q2)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.q1.refresh_from_db()
        self.assertFalse(self.q1.suggest_to_bank)
        self.assertEqual(self.q1.bank_state, Question.BANK_STATE_PRIVATE)


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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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


class QuizResultBlankFieldsStillSerializeTest(TestCase):
    """A missing byline or a blank choice must not 400 the whole result page.

    QuizResultView ends in `serializer.is_valid(raise_exception=True)`, so any
    CharField that forbids blank turns legitimate-but-empty data into a 400 for
    the ENTIRE screen. The student sees one flat error line and there is
    nothing in the UI to suggest which field did it.

    Two fields did exactly that while their neighbours (course_title,
    board_name, correct_choice) already allowed blank:

      * `teacher_name` — the view sends "" whenever `Quiz.created_by` is NULL,
        which the model explicitly permits (bank / seeded / imported sets).
      * `selected_choice` — sends `Choice.text`, which the builder does not
        require to be non-empty.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.student = User.objects.create_user(
            username="bf_s", email="bf_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="B", is_default=True,
        )
        cls.course = Course.objects.create(title="Civics")
        cls.subject = Subject.objects.create(course=cls.course, name="Civics")
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )
        # created_by deliberately omitted — this is the creatorless case.
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, title="Unowned set",
            quiz_type=Quiz.TYPE_PRACTICE, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED, total_marks=1,
        )
        cls.question = Question.objects.create(
            quiz=cls.quiz, text="Blank-choice question", marks=1, order=0,
        )
        # A choice whose text is empty — the one the student will pick.
        cls.blank_choice = Choice.objects.create(
            question=cls.question, text="", is_correct=True,
        )
        Choice.objects.create(question=cls.question, text="other", is_correct=False)

    def _client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def test_creatorless_quiz_with_a_blank_choice_returns_200(self):
        self.assertIsNone(self.quiz.created_by)

        c = self._client()
        c.post(f"/api/quizzes/{self.quiz.id}/start/")
        c.post(
            f"/api/student/quizzes/{self.quiz.id}/submit/",
            {"answers": [{
                "question": str(self.question.id),
                "selected_choice": str(self.blank_choice.id),
            }]},
            format="json",
        )

        r = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        # Before the allow_blank fixes this was a 400 and the student saw
        # "Unable to load result." with no way to tell why.
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["teacher_name"], "")
        self.assertEqual(r.data["questions"][0]["selected_choice"], "")


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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
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


class TeacherQuizListT1RowDataTest(TestCase):
    """T1's rows carry a bank breakdown and a batch count per quiz.

    The trap this pins: `attempts` and `questions` are joined in the same
    queryset, so a Count on questions without distinct=True is multiplied by
    that quiz's attempt count. A 3-question quiz that two students had sat
    would have reported 6 questions in the site bank.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch, Chapter
        from courses.chapter_tags import set_tags

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(
            username="t1_t", email="t1_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Science")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Optics", order=0)
        cls.b1 = Batch.objects.create(course=cls.course, name="9-A", code="9A")
        cls.b2 = Batch.objects.create(course=cls.course, name="9-B", code="9B")

        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Light",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, time_limit_minutes=30,
        )
        cls.quiz.batches.set([cls.b1, cls.b2])
        set_tags(cls.quiz, [(cls.chapter, "", 0)])

        # Three questions in three different bank states.
        for i, state in enumerate([
            Question.BANK_STATE_ACCEPTED,
            Question.BANK_STATE_SUGGESTED,
            Question.BANK_STATE_PRIVATE,
        ]):
            q = Question.objects.create(
                quiz=cls.quiz, text=f"Q{i}", marks=1, order=i,
                explanation="e", suggest_to_bank=(state != Question.BANK_STATE_PRIVATE),
            )
            Question.objects.filter(id=q.id).update(bank_state=state)
            Choice.objects.create(question=q, text="a", is_correct=True)
            Choice.objects.create(question=q, text="b", is_correct=False)

        # Two submitted attempts — the multiplier that used to inflate counts.
        for name in ("s1", "s2"):
            acct = User.objects.create_user(
                username=name, email=f"{name}@test.com", password="x", is_verified=True)
            prof = LearnerProfile.objects.create(
                account=acct, display_name=name, is_default=True)
            QuizAttempt.objects.create(
                quiz=cls.quiz, student=acct, learner_profile=prof,
                status=QuizAttempt.STATUS_SUBMITTED,
                submitted_at=timezone.now(), score=1,
            )

    def _rows(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get("/api/teacher/quizzes/all/")
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def test_bank_counts_are_not_multiplied_by_attempts(self):
        row = self._rows()[0]
        self.assertEqual(row["bank_accepted"], 1)
        self.assertEqual(row["bank_suggested"], 1)
        self.assertEqual(row["bank_private"], 1)
        self.assertEqual(row["bank_changes_requested"], 0)
        # the whole point: 3 questions, 2 attempts, still 3
        self.assertEqual(row["questions_count"], 3)

    def test_batch_count_drives_the_live_for_n_batches_chip(self):
        self.assertEqual(self._rows()[0]["batch_count"], 2)

    def test_rows_carry_their_chapter_tags_and_timing(self):
        row = self._rows()[0]
        self.assertEqual(
            [t["label"] for t in row["chapter_tags"]], ["Optics"])
        self.assertIs(row["no_specific_chapter"], False)
        self.assertEqual(row["time_limit_minutes"], 30)

    def test_stats_endpoint_reports_this_week_against_last(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get("/api/teacher/quizzes/stats/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["attempts_this_week"], 2)
        self.assertEqual(r.data["attempts_last_week"], 0)
        self.assertEqual(r.data["attempts_delta"], 2)

    def test_an_attempt_from_last_week_lands_in_the_previous_bucket(self):
        QuizAttempt.objects.filter(quiz=self.quiz).update(
            submitted_at=timezone.now() - timedelta(days=9))
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get("/api/teacher/quizzes/stats/")
        self.assertEqual(r.data["attempts_this_week"], 0)
        self.assertEqual(r.data["attempts_last_week"], 2)
        self.assertEqual(r.data["attempts_delta"], -2)


class StudentPracticeChaptersTest(TestCase):
    """S1's "Practise by chapter" list, and the rule that keeps it honest.

    Accuracy is computed from GRADED attempts only. If practice counted, then
    practising a weak chapter would raise the number that identified it as
    weak — the weak-area report would measure how much you practised rather
    than what you know.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Chapter, Batch
        from enrollments.models import Enrollment, Subscription

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="pt_t", email="pt_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Algebra", order=0)
        batch = Batch.objects.create(course=cls.course, name="10-A", code="10A")

        cls.account = User.objects.create_user(
            username="kid", email="kid@test.com", password="x", is_verified=True)
        UserRole.objects.create(
            user=cls.account, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.account, display_name="kid", is_default=True)
        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now,
            expires_at=now + timedelta(days=30))
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            batch=batch, status=Enrollment.STATUS_ACTIVE)

        # A graded mock the learner sat: 1 of 2 right → 50%.
        cls.mock = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Algebra mock",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True)
        # Building the Quiz row directly skips QuizCreateSerializer, which is
        # what mirrors the chapter FK into a ContentChapterTag. Migration 0028
        # plus that mirror mean a chaptered quiz ALWAYS has a tag in
        # production, and the practice endpoints now read the tag — so a
        # fixture with an FK and no tag is a state that cannot exist and would
        # test the wrong thing. Any other ORM-level Quiz creation (management
        # command, data import) has to do this too.
        set_tags(cls.mock, [(cls.chapter, "", 0)])
        cls.attempt = QuizAttempt.objects.create(
            quiz=cls.mock, student=cls.account, learner_profile=cls.learner,
            status=QuizAttempt.STATUS_SUBMITTED, submitted_at=now, score=1)
        for i, correct in enumerate([True, False]):
            q = Question.objects.create(
                quiz=cls.mock, text=f"m{i}", marks=1, order=i, explanation="e")
            Question.objects.filter(id=q.id).update(
                bank_state=Question.BANK_STATE_ACCEPTED)
            right = Choice.objects.create(question=q, text="right", is_correct=True)
            wrong = Choice.objects.create(question=q, text="wrong", is_correct=False)
            StudentAnswer.objects.create(
                attempt=cls.attempt, question=q, is_correct=correct,
                selected_choice=right if correct else wrong)

    def _get(self):
        c = APIClient()
        # get_active_profile() reads the JWT's active_profile claim, not a
        # cookie — see accounts/auth_flow.py:146.
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)},
        )
        return c.get("/api/student/practice/chapters/")

    def test_lists_the_chapter_with_its_graded_accuracy_and_bank_supply(self):
        r = self._get()
        self.assertEqual(r.status_code, 200, r.content)
        row = next(x for x in r.data if x["title"] == "Algebra")
        self.assertEqual(row["accuracy"], 50)
        self.assertEqual(row["answered"], 2)
        self.assertEqual(row["available"], 2)

    def test_practice_answers_do_not_move_the_accuracy(self):
        # The rule this endpoint exists to protect. A practice quiz on the
        # same chapter, all correct — accuracy must stay at the graded 50%.
        practice = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title="Algebra practice",
            quiz_type=Quiz.TYPE_PRACTICE, is_assigned=True)
        set_tags(practice, [(self.chapter, "", 0)])
        att = QuizAttempt.objects.create(
            quiz=practice, student=self.account, learner_profile=self.learner,
            status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=timezone.now(), score=3)
        for i in range(3):
            q = Question.objects.create(
                quiz=practice, text=f"p{i}", marks=1, order=i, explanation="e")
            right = Choice.objects.create(question=q, text="right", is_correct=True)
            StudentAnswer.objects.create(
                attempt=att, question=q, is_correct=True, selected_choice=right)

        row = next(x for x in self._get().data if x["title"] == "Algebra")
        self.assertEqual(row["accuracy"], 50, "practice leaked into graded accuracy")
        self.assertEqual(row["answered"], 2)

    def test_an_untried_chapter_reports_null_not_zero(self):
        from courses.models import Chapter
        Chapter.objects.create(subject=self.subject, title="Untouched", order=1)
        row = next(x for x in self._get().data if x["title"] == "Untouched")
        # "never tried" and "got everything wrong" must not look the same.
        self.assertIsNone(row["accuracy"])
        self.assertEqual(row["answered"], 0)

    def test_starting_practice_draws_only_from_the_accepted_bank(self):
        from quizzes.models import PracticeSession
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.post("/api/student/practice/start/",
                   {"chapter_id": str(self.chapter.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(len(r.data["questions"]), 2)  # both mock Qs are accepted

        # The answer key must not leave the server on a start call.
        for q in r.data["questions"]:
            for choice in q["choices"]:
                self.assertNotIn("is_correct", choice)

        session = PracticeSession.objects.get(id=r.data["session_id"])
        self.assertEqual(session.learner_profile_id, self.learner.id)

    def test_practice_writes_no_quiz_attempt_at_all(self):
        # THE structural guarantee. Practice cannot pollute graded analytics
        # because it never touches the tables graded analytics reads — not
        # because six aggregation sites each remember to filter it.
        from quizzes.models import PracticeSession
        before = QuizAttempt.objects.count()
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        c.post("/api/student/practice/start/",
               {"chapter_id": str(self.chapter.id)}, format="json")

        self.assertEqual(QuizAttempt.objects.count(), before)
        self.assertEqual(PracticeSession.objects.count(), 1)
        # and the graded accuracy is untouched
        row = next(x for x in self._get().data if x["title"] == "Algebra")
        self.assertEqual(row["accuracy"], 50)

    def test_a_chapter_with_no_bank_supply_says_so_rather_than_serving_nothing(self):
        from courses.models import Chapter
        empty = Chapter.objects.create(
            subject=self.subject, title="Nothing here", order=5)
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.post("/api/student/practice/start/",
                   {"chapter_id": str(empty.id)}, format="json")
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.data["available"], 0)

    def test_practice_on_a_course_the_learner_is_not_in_is_refused(self):
        from courses.models import Chapter
        other = Course.objects.create(title="Class 12")
        other_subject = Subject.objects.create(course=other, name="Bio")
        foreign = Chapter.objects.create(
            subject=other_subject, title="Genetics", order=0)
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.post("/api/student/practice/start/",
                   {"chapter_id": str(foreign.id)}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def _start(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.post("/api/student/practice/start/",
                   {"chapter_id": str(self.chapter.id)}, format="json")
        return c, r.data

    def test_answering_returns_the_verdict_immediately(self):
        # Practice IS instant feedback — a mock withholds the key, practice
        # would be pointless if it did the same.
        c, data = self._start()
        q = data["questions"][0]
        r = c.post(f"/api/student/practice/{data['session_id']}/answer/",
                   {"question_id": q["id"], "choice_id": q["choices"][0]["id"]},
                   format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("is_correct", r.data)
        self.assertIsNotNone(r.data["correct_choice_id"])
        self.assertEqual(r.data["total"], 2)

    def test_answering_the_same_question_twice_is_refused(self):
        # Overwriting would let a second guess rewrite the first — the same
        # self-flattering loop that keeping practice out of analytics prevents.
        c, data = self._start()
        q = data["questions"][0]
        body = {"question_id": q["id"], "choice_id": q["choices"][0]["id"]}
        url = f"/api/student/practice/{data['session_id']}/answer/"
        self.assertEqual(c.post(url, body, format="json").status_code, 200)
        self.assertEqual(c.post(url, body, format="json").status_code, 400)

    def test_a_question_outside_the_set_is_refused(self):
        # Otherwise a learner could answer any bank question and shape their
        # own weak-area picture.
        from quizzes.models import Question as Q2
        c, data = self._start()
        stray = Q2.objects.create(
            quiz=self.mock, text="not served", marks=1, order=9, explanation="e")
        ch = Choice.objects.create(question=stray, text="a", is_correct=True)
        r = c.post(f"/api/student/practice/{data['session_id']}/answer/",
                   {"question_id": str(stray.id), "choice_id": str(ch.id)},
                   format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_completing_every_question_marks_the_session_done(self):
        from quizzes.models import PracticeSession
        c, data = self._start()
        for q in data["questions"]:
            r = c.post(f"/api/student/practice/{data['session_id']}/answer/",
                       {"question_id": q["id"], "choice_id": q["choices"][0]["id"]},
                       format="json")
        self.assertTrue(r.data["completed"])
        self.assertIsNotNone(
            PracticeSession.objects.get(id=data["session_id"]).completed_at)

    def test_another_learners_session_is_not_answerable(self):
        c, data = self._start()
        intruder_acct = User.objects.create_user(
            username="nosy", email="nosy@test.com", password="x", is_verified=True)
        intruder = LearnerProfile.objects.create(
            account=intruder_acct, display_name="nosy", is_default=True)
        c2 = APIClient()
        c2.force_authenticate(
            user=intruder_acct,
            token={"context": "learner", "active_profile": str(intruder.id)})
        q = data["questions"][0]
        r = c2.post(f"/api/student/practice/{data['session_id']}/answer/",
                    {"question_id": q["id"], "choice_id": q["choices"][0]["id"]},
                    format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_practice_answers_still_never_touch_graded_accuracy(self):
        # End to end: answer a whole practice set correctly, graded stays 50%.
        c, data = self._start()
        for q in data["questions"]:
            c.post(f"/api/student/practice/{data['session_id']}/answer/",
                   {"question_id": q["id"], "choice_id": q["choices"][0]["id"]},
                   format="json")
        row = next(x for x in self._get().data if x["title"] == "Algebra")
        self.assertEqual(row["accuracy"], 50)
        self.assertEqual(row["answered"], 2)

    def test_a_quiz_tagged_to_two_chapters_supplies_both(self):
        """The semantic change made when practice moved off the legacy FK.

        The FK held ONE "primary" chapter, so a quiz spanning two chapters
        only ever supplied questions to the first. Tags carry both, and
        tagging a question set to two chapters is a statement that it belongs
        to both — so each chapter offers those questions for practice.

        Pinned because it is a deliberate behaviour change, not a refactor
        side effect, and because the join it relies on is exactly the shape
        that silently double-counts if the grouping is ever dropped: `total`
        below must stay 2, not 4.
        """
        from courses.models import Chapter

        second = Chapter.objects.create(
            subject=self.subject, title="Quadratics", order=1)
        set_tags(self.mock, [(self.chapter, "", 0), (second, "", 1)])

        rows = {r["title"]: r for r in self._get().data}
        self.assertEqual(rows["Algebra"]["available"], 2)
        self.assertEqual(rows["Quadratics"]["available"], 2,
                         "the second tagged chapter must supply the same set")
        # Graded accuracy is per chapter and must NOT be inflated by the join:
        # 2 answered questions stay 2 in each chapter, never 4.
        self.assertEqual(rows["Algebra"]["answered"], 2)
        self.assertEqual(rows["Quadratics"]["answered"], 2)
        self.assertEqual(rows["Algebra"]["accuracy"], 50)
        self.assertEqual(rows["Quadratics"]["accuracy"], 50)

    def test_a_chapter_from_a_course_the_learner_is_not_in_is_absent(self):
        from courses.models import Chapter
        other = Course.objects.create(title="Class 12")
        other_subject = Subject.objects.create(course=other, name="Bio")
        Chapter.objects.create(subject=other_subject, title="Genetics", order=0)
        self.assertNotIn("Genetics", [x["title"] for x in self._get().data])


class BatchScopeHasNoLeakWithoutTheLegacyFKTest(TestCase):
    """A batch-scoped quiz must never be visible to another batch.

    This is the gate for retiring the `batch` shim. `batches` empty means
    "every batch of the course", so any writer that sets only the shim leaves
    a quiz that LOOKS course-wide to the M2M rule — the shim fallback in
    quizzes/visibility.py is the only thing hiding that today. These tests
    assert the outcome through the real student endpoint, so they hold whether
    or not the fallback exists, and would fail loudly if dropping it ever
    widened a quiz's audience.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment, Subscription

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(
            username="lk_t", email="lk_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.teacher,
                                role=Role.objects.get(name="TEACHER"),
                                is_active=True, is_primary=True)
        cls.course = Course.objects.create(title="Class 7")
        cls.subject = Subject.objects.create(course=cls.course, name="Geo")
        cls.mine = Batch.objects.create(
            course=cls.course, name="7-A", code="7ALK")
        cls.other = Batch.objects.create(
            course=cls.course, name="7-B", code="7BLK")
        TeachingAssignment.objects.create(
            teacher=cls.teacher, subject=cls.subject, batch=cls.mine)

        cls.account = User.objects.create_user(
            username="lk_kid", email="lk_kid@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.account,
                                role=Role.objects.get(name="STUDENT"),
                                is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.account, display_name="lk_kid", is_default=True)
        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now,
            expires_at=now + timedelta(days=30))
        # The learner sits in `mine`.
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            batch=cls.mine, status=Enrollment.STATUS_ACTIVE)

    def _visible_titles(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.get("/api/student/quizzes/", {"course": str(self.course.id)})
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()
        rows = rows["results"] if isinstance(rows, dict) and "results" in rows else rows
        return {row["title"] for row in rows}

    def _quiz(self, title, *, batches=(), batch=None):
        q = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher, title=title,
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True)
        if batches:
            q.batches.set(batches)
        return q

    def test_a_quiz_scoped_to_another_batch_via_the_m2m_is_hidden(self):
        self._quiz("Other batch only", batches=[self.other])
        self.assertNotIn("Other batch only", self._visible_titles())

    def test_a_quiz_scoped_to_my_batch_via_the_m2m_is_visible(self):
        self._quiz("My batch", batches=[self.mine])
        self.assertIn("My batch", self._visible_titles())

    def test_a_truly_course_wide_quiz_is_visible(self):
        # No batch, no batches — the one case where empty legitimately means
        # everyone, and the case the shim fallback must not be confused with.
        self._quiz("Course wide")
        self.assertIn("Course wide", self._visible_titles())

    def test_the_create_endpoint_populates_the_m2m_so_no_shim_is_needed(self):
        # The regression that would reopen the leak: create writing only the
        # shim. Asserted through the API, not the model, so it covers the real
        # serializer path the builder uses.
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id),
            "batch_id": str(self.mine.id),
            "title": "Created scoped",
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        quiz = Quiz.objects.get(id=r.json()["id"])
        self.assertEqual(
            set(quiz.batches.values_list("id", flat=True)), {self.mine.id},
            "create must populate batches, or the shim can never be retired",
        )


class QuizDuplicateCarriesScopeAndChapterTest(TestCase):
    """Duplicating a quiz must not quietly change who sees it, or lose where
    it sits in the syllabus.

    It copied only the legacy single-batch FK, so a multi-batch quiz's copy
    had an EMPTY `batches` set — which quizzes/visibility.py reads as "every
    batch of the course". Today the FK fallback masks that; the moment the FK
    is retired the copy leaks to the whole course. Chapter tags were not
    copied at all, which was invisible because the builder just showed no
    chapter selected.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch, Chapter

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="dup_t", email="dup_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.teacher,
                                role=Role.objects.get(name="TEACHER"),
                                is_active=True, is_primary=True)
        cls.course = Course.objects.create(title="Class 12")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        cls.batch_a = Batch.objects.create(
            course=cls.course, name="12-A", code="12ADUP")
        cls.batch_b = Batch.objects.create(
            course=cls.course, name="12-B", code="12BDUP")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Integrals", order=0)
        TeachingAssignment.objects.create(
            teacher=cls.teacher, subject=cls.subject, batch=cls.batch_a)

    def _duplicate(self, quiz):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.post(f"/api/teacher/quizzes/{quiz.id}/duplicate/")
        self.assertEqual(r.status_code, 201, r.content)
        return Quiz.objects.get(id=r.json()["id"])

    def test_a_two_batch_quiz_copy_keeps_both_batches(self):
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title="Two batch", quiz_type=Quiz.TYPE_MOCK)
        quiz.batches.set([self.batch_a, self.batch_b])

        copy = self._duplicate(quiz)
        self.assertEqual(
            set(copy.batches.values_list("id", flat=True)),
            {self.batch_a.id, self.batch_b.id},
            "an empty batches set on the copy reads as 'every batch of the course'",
        )

    def test_the_copy_keeps_its_chapter(self):
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title="Placed", quiz_type=Quiz.TYPE_MOCK)
        set_tags(quiz, [(self.chapter, "", 0)])

        copy = self._duplicate(quiz)
        self.assertEqual(
            [t.chapter_id for t in copy.chapter_tags.all()], [self.chapter.id])

    def test_a_free_text_tag_and_the_note_survive_the_copy(self):
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title="Noted", quiz_type=Quiz.TYPE_MOCK,
            chapter_note="Bring log tables.")
        set_tags(quiz, [(None, "Revision week", 0)])

        copy = self._duplicate(quiz)
        tags = list(copy.chapter_tags.all())
        self.assertEqual(len(tags), 1)
        self.assertIsNone(tags[0].chapter_id)
        self.assertEqual(tags[0].custom_label, "Revision week")
        self.assertEqual(copy.chapter_note, "Bring log tables.")

    def test_an_unscoped_quiz_copy_stays_unscoped(self):
        # Empty in, empty out — the copy must not invent a scope either.
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title="Course wide", quiz_type=Quiz.TYPE_MOCK)
        copy = self._duplicate(quiz)
        self.assertEqual(copy.batches.count(), 0)


class QuizChapterTagInvariantTest(TestCase):
    """`Quiz.chapter` and ContentChapterTag must not disagree.

    Phase 10 wants the legacy FK dropped. It cannot be while some quizzes
    record their chapter ONLY there — a read moved onto tags would silently
    see "no chapter" for them, which is exactly what happened on S3.
    Migration 0028 repaired existing rows; this pins that new quizzes created
    through the pre-Phase-3 path (chapter_id / custom_chapter, still what the
    builder sends) also get a tag, or the gap reopens on the next create.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch, Chapter

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="ct_t", email="ct_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.teacher,
                                role=Role.objects.get(name="TEACHER"),
                                is_active=True, is_primary=True)
        cls.course = Course.objects.create(title="Class 11")
        cls.subject = Subject.objects.create(course=cls.course, name="Bio")
        cls.batch = Batch.objects.create(
            course=cls.course, name="11-A", code="11ABIO")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Cell Structure", order=0)
        TeachingAssignment.objects.create(
            teacher=cls.teacher, subject=cls.subject, batch=cls.batch)

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def _tags_of(self, quiz):
        from django.contrib.contenttypes.models import ContentType
        from courses.models_chapter_tags import ContentChapterTag
        return list(
            ContentChapterTag.objects.filter(
                content_type=ContentType.objects.get_for_model(Quiz),
                object_id=quiz.id,
            )
        )

    def test_creating_with_chapter_id_also_writes_a_tag(self):
        r = self._client().post("/api/teacher/quizzes/", {
            "title": "Cells quiz",
            "subject": str(self.subject.id),
            "batch_id": str(self.batch.id),
            "chapter_id": str(self.chapter.id),
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        quiz = Quiz.objects.get(id=r.json()["id"])
        # No FK to check any more (Phase 10, quizzes/0030) — the tag IS the
        # placement, which is exactly what this test exists to pin.
        tags = self._tags_of(quiz)
        self.assertEqual(len(tags), 1,
                         "a chapter-only create must still produce one tag")
        self.assertEqual(tags[0].chapter_id, self.chapter.id)

    def test_the_generic_relation_can_be_joined(self):
        # The whole point of declaring GenericRelation on Quiz: the practice
        # endpoints aggregate supply/accuracy GROUPED BY chapter and cannot do
        # that through attach_chapter_tags(), which only serializes a page.
        r = self._client().post("/api/teacher/quizzes/", {
            "title": "Joinable", "subject": str(self.subject.id),
            "batch_id": str(self.batch.id), "chapter_id": str(self.chapter.id),
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        found = Quiz.objects.filter(chapter_tags__chapter=self.chapter)
        self.assertIn(r.json()["id"], [str(q.id) for q in found])

    def test_explicit_chapter_tags_are_not_overwritten_by_the_fk_mirror(self):
        # When the client DOES send chapter_tags, apply_chapter_tags owns the
        # rows and the FK mirror must not fire and flatten them back to one.
        second = self.chapter.__class__.objects.create(
            subject=self.subject, title="Cell Division", order=1)
        r = self._client().post("/api/teacher/quizzes/", {
            "title": "Two chapters",
            "subject": str(self.subject.id),
            "batch_id": str(self.batch.id),
            "chapter_tags": [
                {"chapter_id": str(self.chapter.id)},
                {"chapter_id": str(second.id)},
            ],
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        quiz = Quiz.objects.get(id=r.json()["id"])
        self.assertEqual(len(self._tags_of(quiz)), 2,
                         "the FK mirror must not clobber explicit tags")


class QuizResultS3PayloadTest(TestCase):
    """S3's extra result fields (Phase 9).

    The blank count and the first-attempt verdict are the two that fail
    silently: a wrong blank count is a plausible-looking number, and a
    first attempt reporting an improvement of 0 reads as "no progress"
    rather than "nothing to compare to".
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch, Chapter
        from enrollments.models import Enrollment, Subscription

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")
        cls.teacher = User.objects.create_user(
            username="s3_t", email="s3_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.teacher,
                                role=Role.objects.get(name="TEACHER"),
                                is_active=True, is_primary=True)
        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Civics")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Federalism", order=0)
        batch = Batch.objects.create(course=cls.course, name="10-A", code="10A2")

        cls.account = User.objects.create_user(
            username="s3_kid", email="s3_kid@test.com", password="x", is_verified=True)
        UserRole.objects.create(user=cls.account,
                                role=Role.objects.get(name="STUDENT"),
                                is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.account, display_name="s3_kid", is_default=True)
        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now,
            expires_at=now + timedelta(days=30))
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            batch=batch, status=Enrollment.STATUS_ACTIVE)

        # A 4-mark paper; the learner answers only 2 of the 4 questions.
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Civics mock",
            quiz_type=Quiz.TYPE_MOCK,
            is_assigned=True, total_marks=4, time_limit_minutes=30)
        # S3 reads chapters from ContentChapterTag only — the legacy FK
        # fallback went in Phase 10. Building the row through the ORM skips
        # QuizCreateSerializer's FK→tag mirror, so tag it explicitly (same
        # note as StudentPracticeChaptersTest).
        set_tags(cls.quiz, [(cls.chapter, "", 0)])
        cls.questions = []
        for i in range(4):
            q = Question.objects.create(
                quiz=cls.quiz, text=f"q{i}", marks=1, order=i, explanation="because")
            Choice.objects.create(question=q, text="right", is_correct=True)
            Choice.objects.create(question=q, text="wrong", is_correct=False)
            cls.questions.append(q)

    def _attempt(self, number, right, answered=2, marked=0):
        att = QuizAttempt.objects.create(
            quiz=self.quiz, student=self.account, learner_profile=self.learner,
            status=QuizAttempt.STATUS_SUBMITTED,
            submitted_at=timezone.now(), score=right, attempt_number=number)
        for i in range(answered):
            q = self.questions[i]
            correct = i < right
            StudentAnswer.objects.create(
                attempt=att, question=q, is_correct=correct,
                marked_for_review=(i < marked),
                selected_choice=q.choices.filter(is_correct=correct).first())
        return att

    def _result(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)})
        r = c.get(f"/api/quizzes/{self.quiz.id}/result/")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_blank_count_is_the_paper_minus_what_was_answered(self):
        self._attempt(1, right=1, answered=2)
        d = self._result()
        self.assertEqual(d["questions_total"], 4)
        self.assertEqual(len(d["questions"]), 2)
        self.assertEqual(d["blank_count"], 2, "blanks must count unanswered questions")

    def test_marked_count_reaches_the_filter_chip(self):
        self._attempt(1, right=1, answered=2, marked=1)
        self.assertEqual(self._result()["marked_count"], 1)

    def test_first_attempt_has_no_previous_percent(self):
        # Must be null, NOT 0 — "no improvement" and "nothing to compare to"
        # are different statements and the hero renders them differently.
        self._attempt(1, right=1)
        self.assertIsNone(self._result()["previous_percent"])

    def test_second_attempt_compares_against_the_previous_one(self):
        self._attempt(1, right=1)          # 1/4 = 25%
        self._attempt(2, right=3)          # 3/4 = 75%
        self.assertEqual(self._result()["previous_percent"], 25.0)

    def test_chapters_carry_is_custom_for_the_pencil_marker(self):
        # This quiz has the legacy `chapter` FK and NO ContentChapterTag rows,
        # which is what QuizCreateSerializer actually writes — so this also
        # pins the FK fallback. Without it the section renders empty and looks
        # like a quiz with no chapter.
        self._attempt(1, right=1)
        chapters = self._result()["chapters"]
        self.assertTrue(chapters, "the quiz's chapters must reach S3")
        self.assertEqual(chapters[0]["label"], "Federalism")
        self.assertIs(chapters[0]["is_custom"], False)
        self.assertEqual(chapters[0]["chapter_id"], str(self.chapter.id))

    def test_a_teacher_created_chapter_is_flagged_custom(self):
        from courses.models import Chapter
        custom = Chapter.objects.create(
            subject=self.subject, title="Local governance drive",
            order=1, is_custom=True)
        # Re-tag, not just re-point the FK: the tag is what S3 reads.
        set_tags(self.quiz, [(custom, "", 0)])
        self._attempt(1, right=1)
        chapters = self._result()["chapters"]
        self.assertEqual(chapters[0]["label"], "Local governance drive")
        self.assertIs(chapters[0]["is_custom"], True,
                      "S3 marks teacher-created chapters with a pencil")

    def test_time_taken_and_limit_are_reported(self):
        self._attempt(1, right=1)
        d = self._result()
        self.assertEqual(d["time_limit_minutes"], 30)
        self.assertIsNotNone(d["time_taken_seconds"])
        self.assertGreaterEqual(d["time_taken_seconds"], 0)


class StudentDashboardLastAttemptAtTest(TestCase):
    """`last_attempt_at` on /student/quizzes/ — S1's "Your last attempts" rail.

    The rail pairs each score tile with a "when", and QuizDashboardSerializer
    exposed no timestamp at all before this.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment, Subscription

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="la_t", email="la_t@test.com", password="x", is_verified=True)
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        batch = Batch.objects.create(course=cls.course, name="9-A", code="9A")

        cls.account = User.objects.create_user(
            username="la_kid", email="la_kid@test.com", password="x", is_verified=True)
        UserRole.objects.create(
            user=cls.account, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.account, display_name="la_kid", is_default=True)

        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now,
            expires_at=now + timedelta(days=30))
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.learner, course=cls.course,
            batch=batch, status=Enrollment.STATUS_ACTIVE)

        # batch left NULL = "all batches" (see quizzes/migrations/0021).
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Motion",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True, total_marks=2)
        cls.question = Question.objects.create(
            quiz=cls.quiz, text="q0", marks=1, order=0, explanation="e")
        cls.choice = Choice.objects.create(
            question=cls.question, text="right", is_correct=True)

    def _rows(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.learner.id)},
        )
        r = c.get("/api/student/quizzes/", {"course": str(self.course.id)})
        self.assertEqual(r.status_code, 200, r.content)
        # .json(), not .data — `.data` holds the raw datetime the method
        # field returned, and it is the RENDERED ISO string the rail parses.
        return r.json()

    def _submit(self, when, attempt_number, score=1):
        # An attempt only counts as a real completion if it has >=1 answer —
        # the dashboard's prefetch filters on that (see StudentDashboardView).
        att = QuizAttempt.objects.create(
            quiz=self.quiz, student=self.account, learner_profile=self.learner,
            status=QuizAttempt.STATUS_SUBMITTED, submitted_at=when,
            score=score, attempt_number=attempt_number)
        StudentAnswer.objects.create(
            attempt=att, question=self.question, is_correct=True,
            selected_choice=self.choice)
        return att

    def test_carries_the_teacher_note_for_the_s1_quote(self):
        # S1 renders chapter_note as the quoted line under an assigned row.
        # A missing key is indistinguishable from "no note" in the UI, so
        # this pins the key's presence, not just its value.
        row = next(x for x in self._rows() if x["title"] == "Motion")
        self.assertIn("chapter_note", row)
        Quiz.objects.filter(id=self.quiz.id).update(
            chapter_note="Revise section 3 before you start.")
        row = next(x for x in self._rows() if x["title"] == "Motion")
        self.assertEqual(row["chapter_note"], "Revise section 3 before you start.")

    def test_null_before_any_attempt(self):
        row = next(x for x in self._rows() if x["title"] == "Motion")
        self.assertIn("last_attempt_at", row)
        self.assertIsNone(row["last_attempt_at"])

    def test_reports_the_submitted_time(self):
        when = timezone.now() - timedelta(days=2)
        self._submit(when, attempt_number=1)
        row = next(x for x in self._rows() if x["title"] == "Motion")
        self.assertIsNotNone(row["last_attempt_at"])
        self.assertEqual(row["last_attempt_at"][:10], when.date().isoformat())

    def test_takes_the_newest_time_not_the_highest_attempt_number(self):
        # The prefetch is ordered by -attempt_number, so a naive [0] would
        # answer with attempt 2's time. An earlier attempt auto-submitted
        # late by the expiry sweep carries the NEWER submitted_at, and the
        # rail's "when" must reflect when the learner last actually finished.
        newest = timezone.now()
        self._submit(newest - timedelta(days=5), attempt_number=2)
        self._submit(newest, attempt_number=1)
        row = next(x for x in self._rows() if x["title"] == "Motion")
        self.assertEqual(row["last_attempt_at"][:10], newest.date().isoformat())


class AdminQuestionBankReviewTest(TestCase):
    """A1 · the admin review queue (Phase 7).

    The phase's own done-criterion is end-to-end, not per-endpoint:
    accepting a question must make it appear in ANOTHER teacher's
    scope=school bank, and requesting changes must surface the feedback where
    the first teacher will actually see it.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="ADMIN")

        cls.admin = User.objects.create_user(
            username="adm", email="adm@test.com", password="x",
            is_verified=True, is_staff=True, is_superuser=True,
        )
        cls.author = User.objects.create_user(
            username="wrote_it", email="wrote@test.com", password="x", is_verified=True)
        cls.colleague = User.objects.create_user(
            username="other_t", email="other@test.com", password="x", is_verified=True)
        for u in (cls.author, cls.colleague):
            UserRole.objects.create(
                user=u, role=Role.objects.get(name="TEACHER"),
                is_active=True, is_primary=True)

        from courses.models import Batch

        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        # BOTH teachers teach the subject — that is what makes scope=school
        # meaningful. Each needs their own batch: TeachingAssignment is unique
        # per subject when batch is NULL, so two batch-less rows collide.
        for i, u in enumerate((cls.author, cls.colleague)):
            batch = Batch.objects.create(
                course=cls.course, name=f"10-{i}", code=f"10{i}")
            TeachingAssignment.objects.create(
                subject=cls.subject, teacher=u, batch=batch, is_active=True)

        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.author, title="Algebra")
        cls.q = Question.objects.create(
            quiz=cls.quiz, text="What is x?", marks=1, order=0, explanation="e")
        Choice.objects.create(question=cls.q, text="4", is_correct=True)
        Choice.objects.create(question=cls.q, text="5", is_correct=False)
        # A question the author kept to themselves.
        cls.private_q = Question.objects.create(
            quiz=cls.quiz, text="Class-specific one", marks=1, order=1,
            explanation="e", suggest_to_bank=False)

    def _admin(self):
        c = APIClient()
        c.force_authenticate(user=self.admin, token={"context": "admin"})
        return c

    def _teacher(self, user):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": "teacher"})
        return c

    def test_queue_defaults_to_what_is_actually_waiting(self):
        r = self._admin().get("/api/quizzes/admin/question-bank/queue/")
        self.assertEqual(r.status_code, 200, r.content)
        texts = [q["text"] for q in r.data["results"]]
        self.assertIn("What is x?", texts)
        self.assertEqual(r.data["counts"]["suggested"], 1)

    def test_a_privately_kept_question_never_reaches_the_queue(self):
        # The teacher opted out. Surfacing it here would leak work they
        # explicitly chose not to share.
        r = self._admin().get(
            "/api/quizzes/admin/question-bank/queue/?state=private")
        self.assertNotIn(
            "Class-specific one", [q["text"] for q in r.data["results"]])

    def test_accepting_publishes_it_into_a_colleagues_school_bank(self):
        # Phase 7's done-criterion, first half.
        before = self._teacher(self.colleague).get(
            "/api/teacher/question-bank/?scope=school")
        self.assertEqual(len(before.data), 0)

        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "accept"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        after = self._teacher(self.colleague).get(
            "/api/teacher/question-bank/?scope=school")
        self.assertEqual([q["text"] for q in after.data], ["What is x?"])

    def test_requesting_changes_surfaces_the_note_on_the_teachers_own_bank(self):
        # Phase 7's done-criterion, second half.
        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "request_changes", "feedback": "Option B is ambiguous."},
            format="json")
        self.assertEqual(r.status_code, 200, r.content)

        mine = self._teacher(self.author).get(
            "/api/teacher/question-bank/?scope=mine&state=changes_requested")
        row = mine.data[0]
        self.assertEqual(row["bank_feedback"], "Option B is ambiguous.")

    def test_requesting_changes_without_feedback_is_refused(self):
        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "request_changes"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.q.refresh_from_db()
        self.assertEqual(self.q.bank_state, Question.BANK_STATE_SUGGESTED)

    def test_bulk_accept_is_all_or_nothing(self):
        q2 = Question.objects.create(
            quiz=self.quiz, text="Second", marks=1, order=2, explanation="e")
        r = self._admin().post(
            "/api/quizzes/admin/question-bank/bulk-review/",
            {"question_ids": [str(self.q.id), str(q2.id),
                              "00000000-0000-0000-0000-000000000000"],
             "action": "accept"},
            format="json")
        self.assertEqual(r.status_code, 400, r.content)
        # nothing applied
        self.q.refresh_from_db()
        self.assertEqual(self.q.bank_state, Question.BANK_STATE_SUGGESTED)

    def test_bulk_accept_applies_to_every_named_question(self):
        q2 = Question.objects.create(
            quiz=self.quiz, text="Second", marks=1, order=2, explanation="e")
        r = self._admin().post(
            "/api/quizzes/admin/question-bank/bulk-review/",
            {"question_ids": [str(self.q.id), str(q2.id)], "action": "accept"},
            format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["updated"], 2)
        for q in (self.q, q2):
            q.refresh_from_db()
            self.assertEqual(q.bank_state, Question.BANK_STATE_ACCEPTED)

    def test_an_admin_can_remap_the_question_to_a_real_chapter(self):
        from courses.models import Chapter
        real = Chapter.objects.create(
            subject=self.subject, title="Linear equations", order=0)

        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "accept", "map_to_chapter_id": str(real.id)},
            format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(
            [t["label"] for t in r.data["chapter_tags"]]
            if "chapter_tags" in r.data else [r.data["chapter_label"]],
            ["Linear equations"])

    def test_a_chapter_from_another_subject_is_refused(self):
        from courses.models import Chapter
        other_subject = Subject.objects.create(course=self.course, name="Physics")
        foreign = Chapter.objects.create(
            subject=other_subject, title="Optics", order=0)

        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "accept", "map_to_chapter_id": str(foreign.id)},
            format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_promoting_makes_a_teachers_own_chapter_real(self):
        from courses.models import Chapter
        from courses.chapter_tags import set_tags
        custom = Chapter.objects.create(
            subject=self.subject, title="Board-pattern sums", order=1,
            is_custom=True, created_by=self.author)
        set_tags(self.quiz, [(custom, "", 0)])

        r = self._admin().patch(
            f"/api/quizzes/admin/question-bank/{self.q.id}/review/",
            {"action": "accept", "promote_chapter": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        custom.refresh_from_db()
        self.assertIsNotNone(custom.promoted_at)

    def test_a_teacher_cannot_reach_the_admin_queue(self):
        r = self._teacher(self.author).get(
            "/api/quizzes/admin/question-bank/queue/")
        self.assertIn(r.status_code, (401, 403))


class TeacherBankStatusT4Test(TestCase):
    """T4: the four state counts, the auto-suggest default, and the latest note."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import TeacherProfile

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="t4_t", email="t4_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        TeacherProfile.objects.create(user=cls.teacher)
        cls.course = Course.objects.create(title="Class 12")
        cls.subject = Subject.objects.create(course=cls.course, name="Bio")
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Cells")

        for state, n in [
            (Question.BANK_STATE_ACCEPTED, 2),
            (Question.BANK_STATE_SUGGESTED, 3),
            (Question.BANK_STATE_PRIVATE, 1),
        ]:
            for i in range(n):
                q = Question.objects.create(
                    quiz=cls.quiz, text=f"{state}{i}", marks=1, order=i,
                    explanation="e")
                Question.objects.filter(id=q.id).update(bank_state=state)

        cls.flagged = Question.objects.create(
            quiz=cls.quiz, text="The ambiguous one", marks=1, order=9,
            explanation="e")
        Question.objects.filter(id=cls.flagged.id).update(
            bank_state=Question.BANK_STATE_CHANGES_REQUESTED,
            bank_feedback="Option C is ambiguous — please reword it.",
        )

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def test_state_counts_and_latest_note(self):
        r = self._client().get("/api/teacher/bank-status/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["accepted"], 2)
        self.assertEqual(r.data["suggested"], 3)
        self.assertEqual(r.data["private"], 1)
        self.assertEqual(r.data["changes_requested"], 1)
        # "2 need changes" is a dead end without the admin's actual words.
        self.assertEqual(
            r.data["latest_note"]["feedback"],
            "Option C is ambiguous — please reword it.")
        self.assertEqual(
            r.data["latest_note"]["question_id"], str(self.flagged.id))

    def test_auto_suggest_defaults_on_and_can_be_turned_off(self):
        c = self._client()
        self.assertIs(c.get("/api/teacher/bank-status/").data["auto_suggest_questions"], True)

        r = c.patch("/api/teacher/bank-status/",
                    {"auto_suggest_questions": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(c.get("/api/teacher/bank-status/").data["auto_suggest_questions"], False)

    def test_turning_auto_suggest_off_does_not_rewrite_existing_questions(self):
        # The switch is a default for NEW work. Sweeping existing rows would
        # silently un-suggest what an admin already accepted and wipe a
        # changes-requested conversation.
        c = self._client()
        c.patch("/api/teacher/bank-status/",
                {"auto_suggest_questions": False}, format="json")

        r = c.get("/api/teacher/bank-status/")
        self.assertEqual(r.data["accepted"], 2)
        self.assertEqual(r.data["suggested"], 3)
        self.assertEqual(r.data["changes_requested"], 1)

    def test_a_non_boolean_is_rejected(self):
        r = self._client().patch("/api/teacher/bank-status/",
                                 {"auto_suggest_questions": "yes"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)


class TeacherBankT3RowDataTest(TestCase):
    """T3 shows each question's chapter, and must not pay a query per row.

    A Question has no chapter of its own — Phase 3 put chapter tagging on the
    quiz — so the chip resolves through the quiz. Doing that naively inside
    the serializer is one query per row, which on a real 142-question bank is
    142 extra round trips.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Chapter
        from courses.chapter_tags import set_tags

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="t3_t", email="t3_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.course = Course.objects.create(title="Class 11")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True)
        syllabus = Chapter.objects.create(
            subject=cls.subject, title="Kinematics", order=0)
        custom = Chapter.objects.create(
            subject=cls.subject, title="Board-pattern numericals", order=1,
            is_custom=True, created_by=cls.teacher)

        # Two quizzes so the page spans more than one chapter.
        for i, (chapter, title) in enumerate(
            [(syllabus, "Motion"), (custom, "Extra drill")]
        ):
            quiz = Quiz.objects.create(
                subject=cls.subject, created_by=cls.teacher, title=title,
                review_status=Quiz.REVIEW_DRAFT,
            )
            set_tags(quiz, [(chapter, "", 0)])
            for n in range(3):
                q = Question.objects.create(
                    quiz=quiz, text=f"{title} Q{n}", marks=1, order=n,
                    explanation="e",
                )
                Choice.objects.create(question=q, text="a", is_correct=True)
                Choice.objects.create(question=q, text="b", is_correct=False)

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c

    def test_rows_carry_their_chapter_and_flag_a_custom_one(self):
        r = self._client().get("/api/teacher/question-bank/?scope=mine")
        self.assertEqual(r.status_code, 200, r.content)
        by_label = {}
        for row in r.data:
            by_label.setdefault(row["chapter_label"], set()).add(
                row["chapter_is_custom"])
        self.assertEqual(by_label["Kinematics"], {False})
        # a teacher-typed chapter no admin has promoted gets the warning tint
        self.assertEqual(by_label["Board-pattern numericals"], {True})

    def test_the_chapter_chip_does_not_cost_a_query_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        c = self._client()
        with CaptureQueriesContext(connection) as ctx:
            r = c.get("/api/teacher/question-bank/?scope=mine")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 6)
        # Flat: resolving 6 questions across 2 quizzes must not approach 6+
        # extra queries. Generous ceiling — it is a regression guard, not a
        # measurement of the current exact count.
        self.assertLess(
            len(ctx), 10,
            f"chapter chip looks N+1 again: {len(ctx)} queries for 6 rows",
        )


class QuizDraftChapterRoundTripTest(TestCase):
    """The builder's edit load must return the quiz's chapter tagging.

    Caught by browser-driving the rebuilt builder (Phase 5a), not by any test:
    QuizDetailTeacherSerializer exposed no chapter fields at all, so the
    picker repopulated itself as empty on edit. On its own that was a
    cosmetic read gap — the old builder only sent chapter fields on create.
    The moment the builder started sending `chapter_tags` on PATCH too, the
    empty picker became a destructive write that silently unfiled the quiz
    from every chapter it was tagged with.
    """

    @classmethod
    def setUpTestData(cls):
        # Local imports, matching the convention the rest of this module uses
        # for courses models it needs in only one class.
        from courses.models import Chapter
        from courses.chapter_tags import set_tags

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher = User.objects.create_user(
            username="dr_t", email="dr_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Algebra", order=0)
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Algebra unit test",
            chapter_note="revise identities first",
        )
        set_tags(cls.quiz, [(cls.chapter, "", 0)])
        set_tags(cls.quiz, [(cls.chapter, "", 0), (None, "Mixed revision", 1)])

    def _draft(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        return c.get(f"/api/quizzes/{self.quiz.id}/draft/")

    def test_draft_returns_the_chapter_tags(self):
        r = self._draft()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("chapter_tags", r.data)
        tags = r.data["chapter_tags"]
        # `label` carries the chapter's own title for a chapter-backed tag and
        # the free text for a custom one, so the picker can render a name
        # without a second lookup. `chapter_id` is what distinguishes them —
        # which is exactly what fromChapterPayload() splits on.
        self.assertEqual(
            [(t["chapter_id"], t["label"]) for t in tags],
            [(str(self.chapter.id), "Algebra"), (None, "Mixed revision")],
        )

    def test_draft_returns_the_note_and_the_no_specific_flag(self):
        r = self._draft()
        self.assertEqual(r.data["chapter_note"], "revise identities first")
        self.assertIs(r.data["no_specific_chapter"], False)

    def test_draft_questions_expose_their_bank_opt_in(self):
        # Same read-gap trap as the chapter fields: the builder's per-question
        # switch needs the real value, or it defaults every switch to on and
        # re-suggests questions the teacher deliberately kept private.
        from quizzes.models import Question, Choice
        q = Question.objects.create(
            quiz=self.quiz, text="Private one?", marks=1, order=0,
            explanation="because.", suggest_to_bank=False,
        )
        Choice.objects.create(question=q, text="a", is_correct=True)
        Choice.objects.create(question=q, text="b", is_correct=False)

        r = self._draft()
        row = next(x for x in r.data["questions"] if str(x["id"]) == str(q.id))
        self.assertIs(row["suggest_to_bank"], False)
        self.assertEqual(row["bank_state"], Question.BANK_STATE_PRIVATE)

    def test_the_keys_are_the_ones_the_picker_reads(self):
        # fromChapterPayload() in the teacher app reads exactly these three
        # names off the draft. Renaming any of them here empties the picker
        # without erroring anywhere.
        r = self._draft()
        for key in ("chapter_tags", "no_specific_chapter", "chapter_note"):
            self.assertIn(key, r.data, f"the builder reads `{key}` off the draft")
        self.assertEqual(
            sorted(r.data["chapter_tags"][0].keys()),
            ["chapter_id", "is_custom", "label", "order"],
        )


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
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED,
        )
        cls.quiz_mine.batches.set([cls.batch_mine])
        cls.quiz_other = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="10-B test",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED,
        )
        cls.quiz_other.batches.set([cls.batch_other])
        # Left with NO batches on purpose: that is what course-wide means now.
        cls.quiz_course_wide = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Evergreen test",
            quiz_type=Quiz.TYPE_MOCK, is_assigned=True,
            review_status=Quiz.REVIEW_APPROVED,
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
        # custom_chapter still creates a real Chapter; placement is recorded as
        # a tag now rather than on a dropped FK.
        self.assertEqual(
            [t.chapter_id for t in quiz.chapter_tags.all()], [chapter.id])
        self.assertEqual(
            set(quiz.batches.values_list("id", flat=True)), {self.batch.id})

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
            quiz_type=Quiz.TYPE_MOCK,
        )
        # Give it a real scope and placement, so "a title-only PATCH disturbs
        # neither" is actually being tested rather than comparing two empties.
        quiz.batches.set([self.batch])
        set_tags(quiz, [(chapter, "", 0)])
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/", {"title": "Renamed"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        quiz.refresh_from_db()
        self.assertEqual(quiz.title, "Renamed")
        self.assertEqual(
            set(quiz.batches.values_list("id", flat=True)), {self.batch.id})
        # A title-only PATCH must not disturb the chapter tag.
        self.assertEqual(
            [t.chapter_id for t in quiz.chapter_tags.all()], [chapter.id])


# =====================================================================
# PHASE 1 — assignment decoupled from admin approval
# =====================================================================

class QuizAssignmentDecouplingTest(TestCase):
    """Phase 1: `is_assigned` (teacher-controlled) gates student visibility;
    `review_status` is informational and gates nothing.

    Before this, a teacher could not make their own quiz live for their own
    class without an admin approving it, because every student queryset
    filtered on `is_published` and only AdminQuizReviewView ever set it.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment

        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="ph1_t", email="ph1_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )

        now = timezone.now()
        cls.course = Course.objects.create(title="Class 11")
        cls.subject = Subject.objects.create(course=cls.course, name="Chemistry")
        cls.batch_a = Batch.objects.create(course=cls.course, name="11-A", code="11A")
        cls.batch_b = Batch.objects.create(course=cls.course, name="11-B", code="11B")
        cls.batch_c = Batch.objects.create(course=cls.course, name="11-C", code="11C")
        TeachingAssignment.objects.create(
            batch=cls.batch_a, subject=cls.subject, teacher=cls.teacher, is_active=True,
        )

        # A wholly unrelated course, for the cross-course rejection test.
        cls.other_course = Course.objects.create(title="Class 12")
        cls.other_batch = Batch.objects.create(
            course=cls.other_course, name="12-A", code="12A",
        )

        def make_student(tag, batch):
            user = User.objects.create_user(
                username=f"ph1_{tag}", email=f"ph1_{tag}@test.com",
                password="x", is_verified=True,
            )
            UserRole.objects.create(
                user=user, role=Role.objects.get(name="STUDENT"),
                is_active=True, is_primary=True,
            )
            profile = LearnerProfile.objects.create(
                account=user, display_name=tag.upper(), is_default=True,
            )
            Subscription.objects.create(
                user=user, learner_profile=profile, course=cls.course,
                status=Subscription.STATUS_ACTIVE,
                starts_at=now, expires_at=now + timedelta(days=30),
            )
            Enrollment.objects.create(
                user=user, learner_profile=profile, course=cls.course,
                batch=batch, status=Enrollment.STATUS_ACTIVE,
            )
            return user, profile

        cls.student_a, cls.profile_a = make_student("sa", cls.batch_a)
        cls.student_b, cls.profile_b = make_student("sb", cls.batch_b)
        cls.student_c, cls.profile_c = make_student("sc", cls.batch_c)

    # ── helpers ──────────────────────────────────────────────────────────

    def _student_client(self, user, profile):
        c = APIClient()
        c.force_authenticate(
            user=user,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        return c

    def _teacher_client(self, user=None):
        c = APIClient()
        c.force_authenticate(user=user or self.teacher, token={"context": "teacher"})
        return c

    def _make_quiz(self, title, *, assigned, review_status=Quiz.REVIEW_DRAFT,
                   batches=(), questions=0):
        # No `legacy_batch` any more — Phase 10 dropped the shim (0032), so
        # `batches` is the only scope a fixture can express.
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher, title=title,
            quiz_type=Quiz.TYPE_MOCK, is_assigned=assigned,
            # Deliberately NOT mirrored: these tests must prove visibility
            # keys off is_assigned alone.
            review_status=review_status,
        )
        if batches:
            quiz.batches.set(batches)
        for i in range(questions):
            q = Question.objects.create(quiz=quiz, text=f"Q{i}", marks=1, order=i)
            Choice.objects.create(question=q, text="right", is_correct=True)
            Choice.objects.create(question=q, text="wrong", is_correct=False)
        return quiz

    def _visible_titles(self, user, profile):
        r = self._student_client(user, profile).get(
            f"/api/student/quizzes/?course={self.course.id}"
        )
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        return [row["title"] for row in rows]

    # ── 1 · a DRAFT quiz that is assigned IS visible ──────────────────────

    def test_a_draft_but_assigned_quiz_is_visible_to_its_batch(self):
        """The whole point of Phase 1: no admin involvement required."""
        self._make_quiz("Draft-but-live", assigned=True,
                        review_status=Quiz.REVIEW_DRAFT, batches=[self.batch_a])
        self.assertIn("Draft-but-live", self._visible_titles(self.student_a, self.profile_a))

    # ── 2 · an APPROVED quiz that is NOT assigned is NOT visible ──────────

    def test_an_approved_but_unassigned_quiz_is_invisible(self):
        """review_status must gate nothing. An admin approving a quiz the
        teacher never assigned must not push it at students."""
        quiz = self._make_quiz("Approved-not-assigned", assigned=False,
                               review_status=Quiz.REVIEW_APPROVED,
                               batches=[self.batch_a])
        self.assertNotIn(
            "Approved-not-assigned",
            self._visible_titles(self.student_a, self.profile_a),
        )
        # ...and every per-object door is shut too, not just the list.
        c = self._student_client(self.student_a, self.profile_a)
        self.assertEqual(c.get(f"/api/quizzes/{quiz.id}/").status_code, 404)
        self.assertEqual(c.post(f"/api/quizzes/{quiz.id}/start/").status_code, 404)
        self.assertEqual(
            c.get(f"/api/student/quizzes/{quiz.id}/attempts/").status_code, 404,
        )

    # ── 3 · the M2M duplicate-row trap ───────────────────────────────────

    def test_a_quiz_in_two_batches_appears_exactly_once(self):
        """`Q(batches__isnull=True) | Q(batches=<id>)` over a JOIN would return
        one row PER matching batch. quizzes/visibility.py uses Exists()
        subqueries instead, so there is no join to duplicate."""
        self._make_quiz("Shared A+B", assigned=True,
                        batches=[self.batch_a, self.batch_b], questions=3)
        titles = self._visible_titles(self.student_a, self.profile_a)
        self.assertEqual(titles.count("Shared A+B"), 1, titles)

    def test_the_batch_rule_adds_no_join_so_needs_no_distinct(self):
        """The duplicate-row trap, asserted at the queryset level.

        Worth recording precisely what is and is not a hazard here, because it
        is counter-intuitive and was measured rather than assumed:

          · `Q(batches__isnull=True) | Q(batches=<one id>)` does NOT duplicate.
            Django collapses the OR into ONE left join whose condition
            (`t.batch_id IS NULL OR t.batch_id = X`) can match at most one
            through-row per quiz.
          · `Q(batches=A) | Q(batches=B)` DOES duplicate — 2 rows for a quiz in
            both, and a plain `Count()` alongside it doubles.

        The second shape is the one that matters: dashboard/views.py's
        _quiz_batch_visibility_q ORs one scope term PER COURSE, so a join-based
        rule would land exactly there. Exists() contributes no join at all and
        is immune to both shapes, which is why the rule is built that way.

        Endpoint-level tests cannot see any of this — StudentDashboardView
        already calls .distinct() and annotates Count(..., distinct=True), so
        it masks the bug entirely.
        """
        from django.db.models import Count

        from quizzes.visibility import batch_scope_q, visible_quiz_q

        quiz = self._make_quiz("Raw A+B", assigned=True,
                               batches=[self.batch_a, self.batch_b], questions=3)

        ids = list(
            Quiz.objects.filter(visible_quiz_q(self.batch_a.id))
            .values_list("id", flat=True)
        )
        self.assertEqual(ids.count(quiz.id), 1, ids)

        # The dangerous shape: several scope terms OR'd together, as the
        # dashboard helper builds one per course. A join-based rule returns
        # this quiz twice.
        multi = batch_scope_q(self.batch_a.id) | batch_scope_q(self.batch_b.id)
        ids = list(
            Quiz.objects.filter(Q(is_assigned=True) & multi)
            .values_list("id", flat=True)
        )
        self.assertEqual(ids.count(quiz.id), 1, ids)

        # ...and a plain Count() (deliberately NOT distinct=True) is not
        # multiplied by it: 3 questions, not 3 x 2 batch rows.
        row = (
            Quiz.objects.filter(Q(is_assigned=True) & multi)
            .annotate(n=Count("questions"))
            .get(id=quiz.id)
        )
        self.assertEqual(row.n, 3)

    def test_the_endpoints_questions_count_is_correct_for_a_multi_batch_quiz(self):
        self._make_quiz("Counted A+B", assigned=True,
                        batches=[self.batch_a, self.batch_b], questions=3)
        r = self._student_client(self.student_a, self.profile_a).get(
            f"/api/student/quizzes/?course={self.course.id}"
        )
        rows = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        row = next(x for x in rows if x["title"] == "Counted A+B")
        self.assertEqual(row["questions_count"], 3)

    # ── 4 · empty `batches` means every batch of the course ───────────────

    def test_a_quiz_with_no_batches_is_visible_to_every_batch(self):
        """Empty M2M has to preserve what `batch IS NULL` meant, or every
        pre-existing course-wide quiz vanishes."""
        self._make_quiz("Evergreen", assigned=True, batches=[])
        for user, profile in (
            (self.student_a, self.profile_a),
            (self.student_b, self.profile_b),
            (self.student_c, self.profile_c),
        ):
            self.assertIn("Evergreen", self._visible_titles(user, profile))

    # REMOVED: test_a_legacy_fk_only_quiz_is_still_batch_scoped.
    #
    # It asserted that a quiz with ONLY the legacy `batch` FK stayed
    # batch-scoped, guarding the fallback in quizzes/visibility.py. Its premise
    # — "QuizCreateSerializer still writes only the legacy FK" — is what Phase
    # 10 fixed: create and duplicate both populate `batches`, 0031 backfilled
    # the stragglers, and 0032 dropped the column, so the state it constructed
    # can no longer exist. The invariant that replaces it is
    # BatchScopeHasNoLeakWithoutTheLegacyFKTest, which asserts through the real
    # student endpoint that another batch's quiz stays hidden and that create
    # populates the M2M — the two facts that made removing the fallback safe.

    # ── 5 · cross-batch isolation, list AND StudentQuizAttemptsView ───────

    def test_another_batch_cannot_see_or_probe_an_a_only_quiz(self):
        quiz = self._make_quiz("A only", assigned=True, batches=[self.batch_a])

        self.assertNotIn("A only", self._visible_titles(self.student_b, self.profile_b))

        # StudentQuizAttemptsView had NO batch check at all before Phase 1 —
        # it handed a Batch-B learner the quiz title, type and total_marks.
        r = self._student_client(self.student_b, self.profile_b).get(
            f"/api/student/quizzes/{quiz.id}/attempts/"
        )
        self.assertEqual(r.status_code, 404, r.content)

        # The owning batch still gets through, so the fix isn't just "deny".
        r = self._student_client(self.student_a, self.profile_a).get(
            f"/api/student/quizzes/{quiz.id}/attempts/"
        )
        self.assertEqual(r.status_code, 200, r.content)

    # ── 6 · the assign endpoint rejects a foreign batch ───────────────────

    def test_assign_rejects_a_batch_from_another_course(self):
        quiz = self._make_quiz("Cross-course attempt", assigned=False)
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True, "batch_ids": [str(self.other_batch.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        quiz.refresh_from_db()
        self.assertFalse(quiz.is_assigned)
        self.assertEqual(quiz.batches.count(), 0)

    def test_assign_rejects_a_foreign_batch_mixed_in_with_a_valid_one(self):
        """Partial validation would be worse than none: the valid batch would
        be assigned and the caller would think the whole call failed."""
        quiz = self._make_quiz("Mixed", assigned=False)
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True,
             "batch_ids": [str(self.batch_a.id), str(self.other_batch.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        quiz.refresh_from_db()
        self.assertEqual(quiz.batches.count(), 0)

    # ── 7 · only the owning teacher may assign ────────────────────────────

    def test_another_teacher_cannot_assign_someone_elses_quiz(self):
        other = User.objects.create_user(
            username="ph1_t2", email="ph1_t2@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=other, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        quiz = self._make_quiz("Not yours", assigned=False)
        r = self._teacher_client(other).patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True, "batch_ids": [str(self.batch_a.id)]},
            format="json",
        )
        self.assertIn(r.status_code, (403, 404), r.content)
        quiz.refresh_from_db()
        self.assertFalse(quiz.is_assigned)

    def test_a_student_cannot_assign(self):
        quiz = self._make_quiz("Student attempt", assigned=False)
        r = self._student_client(self.student_a, self.profile_a).patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True}, format="json",
        )
        self.assertIn(r.status_code, (401, 403, 404), r.content)

    # ── the assign endpoint's happy path + the legacy shims ──────────────

    def test_assigning_makes_a_draft_quiz_live_and_sets_its_scope(self):
        quiz = self._make_quiz("Assign me", assigned=False, questions=2)
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True, "batch_ids": [str(self.batch_a.id), str(self.batch_b.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["is_assigned"])

        quiz.refresh_from_db()
        self.assertTrue(quiz.is_assigned)
        # review_status untouched — assigning is the teacher's own action and
        # must not fake an admin verdict. (The is_published mirror this used to
        # assert was dropped in Phase 10.)
        self.assertEqual(quiz.review_status, Quiz.REVIEW_DRAFT)
        # The M2M is the whole scope — the single-batch shim this used to
        # mirror into was dropped in Phase 10 (0032).
        self.assertEqual(
            set(quiz.batches.values_list("id", flat=True)),
            {self.batch_a.id, self.batch_b.id},
        )
        # Both batches can see it; the third cannot.
        self.assertIn("Assign me", self._visible_titles(self.student_a, self.profile_a))
        self.assertIn("Assign me", self._visible_titles(self.student_b, self.profile_b))
        self.assertNotIn("Assign me", self._visible_titles(self.student_c, self.profile_c))

    def test_assigning_with_an_empty_batch_list_is_course_wide(self):
        quiz = self._make_quiz("Everyone", assigned=False,
                               batches=[self.batch_a])
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True, "batch_ids": []}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        quiz.refresh_from_db()
        # Empty means course-wide, and with the shim gone that is the ONLY
        # thing empty can mean — no FK left to disagree.
        self.assertEqual(quiz.batches.count(), 0)
        self.assertIn("Everyone", self._visible_titles(self.student_c, self.profile_c))

    def test_unassigning_hides_the_quiz_again(self):
        quiz = self._make_quiz("Temporary", assigned=True, batches=[self.batch_a])
        r = self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": False}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        quiz.refresh_from_db()
        self.assertFalse(quiz.is_assigned)
        self.assertNotIn("Temporary", self._visible_titles(self.student_a, self.profile_a))

    def test_omitting_batch_ids_leaves_the_existing_scope_alone(self):
        """A bare {"assign": false} must not silently widen a batch-scoped quiz
        to the whole course — the next re-assign would leak it to every batch."""
        quiz = self._make_quiz("Keep my scope", assigned=True, batches=[self.batch_a])
        self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": False}, format="json",
        )
        quiz.refresh_from_db()
        self.assertEqual(
            set(quiz.batches.values_list("id", flat=True)), {self.batch_a.id},
        )
        self._teacher_client().patch(
            f"/api/teacher/quizzes/{quiz.id}/assign/",
            {"assign": True}, format="json",
        )
        self.assertNotIn("Keep my scope",
                         self._visible_titles(self.student_b, self.profile_b))

    def test_publish_and_submit_for_review_still_work_and_do_not_assign(self):
        """Phase 1 must not break the two legacy routes, and neither of them
        may make a quiz visible any more."""
        quiz = self._make_quiz("Review me", assigned=False, questions=1)
        for route in ("publish", "submit-for-review"):
            quiz.review_status = Quiz.REVIEW_DRAFT
            quiz.save(update_fields=["review_status"])
            r = self._teacher_client().patch(
                f"/api/teacher/quizzes/{quiz.id}/{route}/", {}, format="json",
            )
            self.assertEqual(r.status_code, 200, f"{route}: {r.content}")
            quiz.refresh_from_db()
            self.assertEqual(quiz.review_status, Quiz.REVIEW_PENDING)
            self.assertFalse(quiz.is_assigned, route)
        self.assertNotIn("Review me",
                         self._visible_titles(self.student_a, self.profile_a))


# REMOVED: QuizBackfillInvariantTest.
#
# It replayed Phase 1's migrations 0020 and 0021 against live model objects to
# prove the backfills cost no student any access they had before. Both inputs
# are gone: 0020 read `is_published` (dropped, 0029) and 0021 read the `batch`
# shim (dropped, 0032), and the class fed them those values through the CURRENT
# model registry — so it can no longer build its own pre-migration state.
#
# Both migrations ran in every environment long ago and their reasoning is
# preserved in their own docstrings. The invariants they protected are now
# covered by tests that assert OUTCOMES rather than replaying migrations:
# QuizAssignmentDecouplingTest (visibility keys off is_assigned alone) and
# BatchScopeHasNoLeakWithoutTheLegacyFKTest (batch scope holds with no shim).


class QuestionBankStateInvariantTest(TestCase):
    """Question.save()'s invariant: suggest_to_bank=False always forces
    bank_state="private", and turning it on never overwrites an admin's
    existing accept/request-changes decision."""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="qbs_t", email="qbs_t@test.com", password="x",
        )
        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Geography")
        cls.quiz = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Rivers",
            quiz_type=Quiz.TYPE_MOCK,
        )

    def test_new_question_defaults_to_suggested_and_suggest_true(self):
        q = Question.objects.create(
            quiz=self.quiz, text="Longest river?", marks=1, order=0,
        )
        self.assertTrue(q.suggest_to_bank)
        self.assertEqual(q.bank_state, Question.BANK_STATE_SUGGESTED)

    def test_suggest_to_bank_false_forces_private_on_create(self):
        q = Question.objects.create(
            quiz=self.quiz, text="Opted out from the start", marks=1, order=1,
            suggest_to_bank=False,
        )
        self.assertEqual(q.bank_state, Question.BANK_STATE_PRIVATE)

    def test_turning_suggest_to_bank_off_moves_an_accepted_question_to_private(self):
        """Turning the flag off always wins, even over an admin's prior
        "accepted" decision — see README "Interactions & behaviour"."""
        q = Question.objects.create(
            quiz=self.quiz, text="Already accepted", marks=1, order=2,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )
        self.assertEqual(q.bank_state, Question.BANK_STATE_ACCEPTED)

        q.suggest_to_bank = False
        q.save()
        q.refresh_from_db()
        self.assertEqual(q.bank_state, Question.BANK_STATE_PRIVATE)

    def test_accepted_question_is_not_downgraded_by_an_unrelated_save(self):
        """The admin-decision-clobber guard. Without the `elif` excluding
        accepted/changes_requested in Question.save(), a teacher fixing a
        typo on an already-accepted question would silently revert it to
        "suggested" — discarding real curation work — on every single save."""
        q = Question.objects.create(
            quiz=self.quiz, text="Typo hree", marks=1, order=3,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )
        self.assertEqual(q.bank_state, Question.BANK_STATE_ACCEPTED)

        q.text = "Typo here"
        q.save()
        q.refresh_from_db()
        self.assertEqual(q.text, "Typo here")
        self.assertEqual(q.bank_state, Question.BANK_STATE_ACCEPTED)

    def test_changes_requested_question_is_also_not_downgraded(self):
        q = Question.objects.create(
            quiz=self.quiz, text="Needs a fix", marks=1, order=4,
            bank_state=Question.BANK_STATE_CHANGES_REQUESTED,
        )
        q.marks = 2
        q.save()
        q.refresh_from_db()
        self.assertEqual(q.bank_state, Question.BANK_STATE_CHANGES_REQUESTED)


class QuestionBankStateBackfillTest(TestCase):
    """Migration 0023 must derive bank_state from quiz.review_status — NOT
    from is_published, unlike Phase 1's 0020. review_status IS the real gate
    the pre-Phase-2 bank endpoints filtered on (see 0023's own docstring for
    the full reasoning); this test proves the backfill matches that gate
    exactly, for all four review_status values."""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="qbf_t", email="qbf_t@test.com", password="x",
        )
        cls.course = Course.objects.create(title="Class 7")
        cls.subject = Subject.objects.create(course=cls.course, name="Civics")

    def _quiz(self, review_status, title):
        return Quiz.objects.create(
            subject=self.subject, created_by=self.teacher, title=title,
            quiz_type=Quiz.TYPE_MOCK, review_status=review_status,
        )

    def test_backfill_maps_approved_to_accepted_and_everything_else_to_suggested(self):
        from importlib import import_module
        from django.apps import apps as real_apps

        pairs = [
            (self._quiz(Quiz.REVIEW_APPROVED, "Approved"), Question.BANK_STATE_ACCEPTED),
            (self._quiz(Quiz.REVIEW_DRAFT, "Draft"), Question.BANK_STATE_SUGGESTED),
            (self._quiz(Quiz.REVIEW_PENDING, "Pending"), Question.BANK_STATE_SUGGESTED),
            (self._quiz(Quiz.REVIEW_REJECTED, "Rejected"), Question.BANK_STATE_SUGGESTED),
        ]
        questions = [
            Question.objects.create(quiz=quiz, text=quiz.title, marks=1, order=0)
            for quiz, _ in pairs
        ]

        import_module(
            "quizzes.migrations.0023_backfill_question_bank_state"
        ).backfill_bank_state(real_apps, None)

        for question, (quiz, expected) in zip(questions, pairs):
            question.refresh_from_db()
            self.assertEqual(
                question.bank_state, expected,
                f"{quiz.title} (review_status={quiz.review_status})",
            )
            # suggest_to_bank is untouched — stays at its schema default of
            # True for every existing row; nothing becomes private here.
            self.assertTrue(question.suggest_to_bank)


class TeacherQuestionBankOwnershipTest(TestCase):
    """Phase 2's central fix: `scope=mine` is ownership-gated, not
    admin-approval-gated. Also covers the state filter, the summary
    endpoint, the PATCH opt-out endpoint, and the "additive-only" response
    shape contract the existing teacher QuizBank.jsx screen depends on."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        teacher_role = Role.objects.get(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="tqb_t1", email="tqb_t1@test.com", password="x",
            is_verified=True,
        )
        cls.other_teacher = User.objects.create_user(
            username="tqb_t2", email="tqb_t2@test.com", password="x",
            is_verified=True,
        )
        for u in (cls.teacher, cls.other_teacher):
            UserRole.objects.create(
                user=u, role=teacher_role, is_active=True, is_primary=True,
            )

        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        # ASSISTANT: a course-wide PRIMARY slot is unique per subject, and
        # cls.teacher already holds it — see uniq_active_primary_per_subject_
        # courselevel on TeachingAssignment.
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.other_teacher, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT,
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _teacher_client(self, user):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": "teacher"})
        return c

    def _results(self, response):
        data = response.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def _quiz(self, owner, review_status=Quiz.REVIEW_DRAFT, title="Quiz"):
        return Quiz.objects.create(
            subject=self.subject, created_by=owner, title=title,
            quiz_type=Quiz.TYPE_MOCK, review_status=review_status,
        )

    # ── scope=mine: ownership, not approval ─────────────────────────────

    def test_scope_mine_returns_questions_from_a_non_approved_quiz(self):
        """Before this fix, TeacherQuestionBankView's base queryset filtered
        quiz__review_status=Quiz.REVIEW_APPROVED BEFORE the scope branch, so
        a teacher's own question on a draft/pending/rejected quiz was
        invisible even under scope=mine — the exact ownership inversion
        README T3 exists to remove ("Everything you write lands here
        automatically"). Asserting the new behaviour explicitly: this must
        now return the question.
        """
        draft_quiz = self._quiz(self.teacher, Quiz.REVIEW_DRAFT, "My draft")
        q = Question.objects.create(
            quiz=draft_quiz, text="Unapproved but mine", marks=1, order=0,
        )

        r = self._teacher_client(self.teacher).get(
            "/api/teacher/question-bank/?scope=mine"
        )
        self.assertEqual(r.status_code, 200, r.content)
        ids = [row["id"] for row in self._results(r)]
        self.assertIn(str(q.id), ids)

    def test_scope_mine_never_returns_another_teachers_questions(self):
        other_quiz = self._quiz(self.other_teacher, Quiz.REVIEW_APPROVED, "Not mine")
        Question.objects.create(
            quiz=other_quiz, text="Someone else's", marks=1, order=0,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )

        r = self._teacher_client(self.teacher).get(
            "/api/teacher/question-bank/?scope=mine"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._results(r), [])

    # ── scope=school: bank_state="accepted", not "on an approved quiz" ──

    def test_scope_school_returns_only_accepted_bank_state(self):
        approved_quiz = self._quiz(
            self.other_teacher, Quiz.REVIEW_APPROVED, "Other's approved",
        )
        accepted_q = Question.objects.create(
            quiz=approved_quiz, text="Accepted", marks=1, order=0,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )
        # On the SAME approved quiz, but the teacher opted it out — this must
        # NOT surface in school scope just because the quiz was approved.
        private_q = Question.objects.create(
            quiz=approved_quiz, text="Private despite approved quiz",
            marks=1, order=1, suggest_to_bank=False,
        )

        r = self._teacher_client(self.teacher).get(
            "/api/teacher/question-bank/?scope=school"
        )
        self.assertEqual(r.status_code, 200, r.content)
        ids = [row["id"] for row in self._results(r)]
        self.assertIn(str(accepted_q.id), ids)
        self.assertNotIn(str(private_q.id), ids)

    def test_scope_school_excludes_the_requesters_own_questions(self):
        own_quiz = self._quiz(self.teacher, Quiz.REVIEW_APPROVED, "Mine, approved")
        own_accepted = Question.objects.create(
            quiz=own_quiz, text="Mine", marks=1, order=0,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )

        r = self._teacher_client(self.teacher).get(
            "/api/teacher/question-bank/?scope=school"
        )
        self.assertEqual(r.status_code, 200, r.content)
        ids = [row["id"] for row in self._results(r)]
        self.assertNotIn(str(own_accepted.id), ids)

    # ── state= filter ────────────────────────────────────────────────────

    def test_state_filter_narrows_to_each_bank_state(self):
        quiz = self._quiz(self.teacher, title="Mixed states")
        by_state = {
            Question.BANK_STATE_PRIVATE: Question.objects.create(
                quiz=quiz, text="P", marks=1, order=0, suggest_to_bank=False,
            ),
            Question.BANK_STATE_SUGGESTED: Question.objects.create(
                quiz=quiz, text="S", marks=1, order=1,
            ),
            Question.BANK_STATE_ACCEPTED: Question.objects.create(
                quiz=quiz, text="A", marks=1, order=2,
                bank_state=Question.BANK_STATE_ACCEPTED,
            ),
            Question.BANK_STATE_CHANGES_REQUESTED: Question.objects.create(
                quiz=quiz, text="C", marks=1, order=3,
                bank_state=Question.BANK_STATE_CHANGES_REQUESTED,
            ),
        }

        client = self._teacher_client(self.teacher)
        for state, expected in by_state.items():
            r = client.get(f"/api/teacher/question-bank/?scope=mine&state={state}")
            self.assertEqual(r.status_code, 200, r.content)
            ids = [row["id"] for row in self._results(r)]
            self.assertEqual(ids, [str(expected.id)], f"state={state} -> {ids}")

    # ── summary ──────────────────────────────────────────────────────────

    def test_summary_counts_are_correct_and_scoped_to_the_requester(self):
        quiz = self._quiz(self.teacher, title="Mine")
        Question.objects.create(quiz=quiz, text="P1", marks=1, order=0, suggest_to_bank=False)
        Question.objects.create(quiz=quiz, text="P2", marks=1, order=1, suggest_to_bank=False)
        Question.objects.create(quiz=quiz, text="S1", marks=1, order=2)
        Question.objects.create(
            quiz=quiz, text="A1", marks=1, order=3, bank_state=Question.BANK_STATE_ACCEPTED,
        )
        Question.objects.create(
            quiz=quiz, text="A2", marks=1, order=4, bank_state=Question.BANK_STATE_ACCEPTED,
        )
        Question.objects.create(
            quiz=quiz, text="A3", marks=1, order=5, bank_state=Question.BANK_STATE_ACCEPTED,
        )
        Question.objects.create(
            quiz=quiz, text="C1", marks=1, order=6,
            bank_state=Question.BANK_STATE_CHANGES_REQUESTED,
        )

        other_quiz = self._quiz(self.other_teacher, title="Not mine")
        Question.objects.create(
            quiz=other_quiz, text="Other's", marks=1, order=0,
            bank_state=Question.BANK_STATE_ACCEPTED,
        )

        r = self._teacher_client(self.teacher).get("/api/teacher/question-bank/summary/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data, {
            "total": 7, "accepted": 3, "suggested": 1,
            "changes_requested": 1, "private": 2,
        })

    # ── PATCH .../bank/ ──────────────────────────────────────────────────

    def test_only_the_owning_teacher_can_patch_the_bank_flag(self):
        quiz = self._quiz(self.teacher, title="Mine to toggle")
        q = Question.objects.create(quiz=quiz, text="Q", marks=1, order=0)

        r = self._teacher_client(self.other_teacher).patch(
            f"/api/teacher/questions/{q.id}/bank/",
            {"suggest_to_bank": False}, format="json",
        )
        self.assertIn(r.status_code, (403, 404), r.content)
        q.refresh_from_db()
        self.assertTrue(q.suggest_to_bank)
        self.assertEqual(q.bank_state, Question.BANK_STATE_SUGGESTED)

    def test_owning_teacher_can_patch_and_it_moves_the_question_to_private(self):
        quiz = self._quiz(self.teacher, title="Mine to toggle 2")
        q = Question.objects.create(quiz=quiz, text="Q", marks=1, order=0)

        r = self._teacher_client(self.teacher).patch(
            f"/api/teacher/questions/{q.id}/bank/",
            {"suggest_to_bank": False}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["suggest_to_bank"], False)
        self.assertEqual(r.data["bank_state"], Question.BANK_STATE_PRIVATE)
        q.refresh_from_db()
        self.assertFalse(q.suggest_to_bank)
        self.assertEqual(q.bank_state, Question.BANK_STATE_PRIVATE)

    # ── response shape: additive only ───────────────────────────────────

    def test_bank_response_shape_is_additive_only(self):
        """BUILD_GUIDE's "done when": the existing Quiz Bank screen still
        renders unchanged. Every field the pre-Phase-2 serializer returned
        must still be present, unrenamed and untyped-away."""
        quiz = self._quiz(self.teacher, title="Shape check")
        Question.objects.create(
            quiz=quiz, text="Q", marks=1, order=0, explanation="Because",
            topic="Motion", difficulty=Question.DIFFICULTY_EASY,
        )

        r = self._teacher_client(self.teacher).get(
            "/api/teacher/question-bank/?scope=mine"
        )
        self.assertEqual(r.status_code, 200, r.content)
        row = self._results(r)[0]

        pre_existing_fields = {
            "id", "text", "marks", "explanation", "topic", "difficulty",
            "choices", "quiz_id", "quiz_title", "subject_id", "subject_name",
            "author_name", "author_id", "created_at",
        }
        missing = pre_existing_fields - set(row.keys())
        self.assertEqual(missing, set(), f"pre-existing fields dropped: {missing}")

        # And the new fields are present too, additively.
        for new_field in ("bank_state", "suggest_to_bank", "bank_feedback"):
            self.assertIn(new_field, row)


# =========================================================================
# Phase 4 · mock-test model: negative marking, attempt quota, sections
# =========================================================================


class _Phase4Base(TestCase):
    """Shared fixture: one teacher who teaches one subject in one batch, and
    one enrolled, subscribed learner profile."""

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch
        from enrollments.models import Enrollment

        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="p4_t", email="p4_t@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.other_teacher = User.objects.create_user(
            username="p4_t2", email="p4_t2@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.other_teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )

        cls.student = User.objects.create_user(
            username="p4_s", email="p4_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.student, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.student, display_name="S", is_default=True,
        )

        now = timezone.now()
        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.batch = Batch.objects.create(course=cls.course, name="10-A", code="P4A")
        TeachingAssignment.objects.create(
            batch=cls.batch, subject=cls.subject, teacher=cls.teacher, is_active=True,
        )
        Subscription.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=now, expires_at=now + timedelta(days=30),
        )
        Enrollment.objects.create(
            user=cls.student, learner_profile=cls.profile, course=cls.course,
            batch=cls.batch, status=Enrollment.STATUS_ACTIVE,
        )

    # ── clients ──────────────────────────────────────────────────────────

    def _student(self):
        c = APIClient()
        c.force_authenticate(
            user=self.student,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _teacher(self, user=None):
        c = APIClient()
        c.force_authenticate(user=user or self.teacher, token={"context": "teacher"})
        return c

    # ── fixtures ─────────────────────────────────────────────────────────

    def _quiz(self, *, quiz_type=Quiz.TYPE_MOCK, negative="0", questions=4,
              max_attempts=None, reveal=None, marks=1):
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher,
            title=f"{quiz_type} paper", quiz_type=quiz_type,
            negative_marks_per_wrong=Decimal(str(negative)),
            max_attempts=max_attempts,
            reveal_answers=reveal or (
                Quiz.REVEAL_AFTER_SUBMIT if quiz_type == Quiz.TYPE_MOCK
                else Quiz.REVEAL_AFTER_EACH
            ),
            is_assigned=True, review_status=Quiz.REVIEW_DRAFT,
            total_marks=questions * marks,
        )
        self.qs = []
        for i in range(questions):
            q = Question.objects.create(
                quiz=quiz, text=f"Q{i}", marks=marks, order=i,
                explanation="Because",
            )
            q.right = Choice.objects.create(question=q, text="right", is_correct=True)
            q.wrong = Choice.objects.create(question=q, text="wrong", is_correct=False)
            self.qs.append(q)
        return quiz

    def _submit(self, quiz, answers, *, new_attempt=False, client=None):
        """`answers` is a list of (question, choice_or_None). A question left
        out of the list entirely, or passed with None, is a BLANK."""
        c = client or self._student()
        start = c.post(
            f"/api/quizzes/{quiz.id}/start/",
            {"new_attempt": True} if new_attempt else {}, format="json",
        )
        self.assertEqual(start.status_code, 200, start.content)
        payload = [
            {"question": str(q.id),
             "selected_choice": str(ch.id) if ch is not None else None}
            for q, ch in answers
        ]
        r = c.post(
            f"/api/student/quizzes/{quiz.id}/submit/",
            {"answers": payload}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        return (
            QuizAttempt.objects
            .filter(quiz=quiz, learner_profile=self.profile,
                    status=QuizAttempt.STATUS_SUBMITTED)
            .order_by("-attempt_number")
            .first()
        )


class MockNegativeMarkingTest(_Phase4Base):
    """Negative marking applies to mock tests only, never to blanks, and the
    arithmetic is exact."""

    def test_mock_with_two_wrong_at_quarter_mark_scores_correctly(self):
        """BUILD_GUIDE Phase 4 item 5, case 1."""
        quiz = self._quiz(negative="0.25", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].right), (q[1], q[1].right),
            (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        # 2 correct × 1 mark − 2 wrong × 0.25 = 1.5
        self.assertEqual(attempt.score, 1.5)

    def test_practice_attempt_never_subtracts(self):
        """BUILD_GUIDE Phase 4 item 5, case 2. The penalty is deliberately
        left STORED on the practice quiz — the point is that quiz_type, not
        the stored value, decides whether it is applied."""
        quiz = self._quiz(quiz_type=Quiz.TYPE_PRACTICE, negative="0.5", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].right),
            (q[1], q[1].wrong), (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        self.assertEqual(attempt.score, 1.0)
        # And the value really is on the row — this is not passing because
        # the fixture forgot to set it.
        quiz.refresh_from_db()
        self.assertEqual(quiz.negative_marks_per_wrong, Decimal("0.50"))

    def test_blank_answers_are_not_penalised(self):
        """Both spellings of "blank": omitted from the payload entirely, and
        present with selected_choice=null (what the mock screen sends for a
        visited-but-skipped question)."""
        quiz = self._quiz(negative="0.25", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].right),
            (q[1], q[1].wrong),
            (q[2], None),          # explicit null
            # q[3] omitted entirely
        ])
        # 1 − 0.25, with nothing deducted for the two blanks.
        self.assertEqual(attempt.score, 0.75)
        # Neither blank produced a StudentAnswer row.
        self.assertEqual(attempt.answers.count(), 2)

    def test_zero_negative_marking_behaves_like_no_negative_marking(self):
        quiz = self._quiz(negative="0", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].right), (q[1], q[1].right),
            (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        self.assertEqual(attempt.score, 2.0)

    def test_three_wrong_at_point_33_is_exactly_0_99(self):
        """Decimal, not float. In binary floating point 3 × 0.33 is
        0.9899999999999999, so a float implementation stores 0.010000000000000009
        here and fails this assertion."""
        quiz = self._quiz(negative="0.33", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].right),
            (q[1], q[1].wrong), (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        self.assertEqual(attempt.score, 0.01)
        self.assertEqual(Decimal(str(attempt.score)), Decimal("0.01"))
        # Proof the float route would NOT have satisfied the above.
        self.assertNotEqual(1 - 3 * 0.33, 0.01)

    def test_a_mock_total_may_go_negative(self):
        """PRODUCT DECISION, flagged in the Phase 4 handoff: the total is NOT
        floored at zero. Standard for the Indian competitive exams this
        platform serves, and honest — a clamped 0 would tell a learner who
        guessed badly the same thing it tells one who answered nothing."""
        quiz = self._quiz(negative="1", questions=4)
        q = self.qs
        attempt = self._submit(quiz, [
            (q[0], q[0].wrong), (q[1], q[1].wrong), (q[2], q[2].wrong),
        ])
        self.assertEqual(attempt.score, -3.0)

    def test_result_endpoint_reports_the_fractional_score(self):
        """Regression: QuizResultSerializer.score was an IntegerField, which
        truncated 2.75 → 2 on the way out — the screen would have disagreed
        with the stored score."""
        quiz = self._quiz(negative="0.25", questions=4)
        q = self.qs
        self._submit(quiz, [
            (q[0], q[0].right), (q[1], q[1].right), (q[2], q[2].right),
            (q[3], q[3].wrong),
        ])
        r = self._student().get(f"/api/quizzes/{quiz.id}/result/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["score"], 2.75)

    def test_a_flat_practice_quiz_is_untouched_by_any_of_this(self):
        """A practice quiz created the pre-Phase-4 way: no sections, default
        penalty, no attempt cap. Everything must behave exactly as before."""
        quiz = Quiz.objects.create(
            subject=self.subject, created_by=self.teacher, title="Legacy practice",
            quiz_type=Quiz.TYPE_PRACTICE, is_assigned=True, total_marks=2,
        )
        for i in range(2):
            q = Question.objects.create(
                quiz=quiz, text=f"L{i}", marks=1, order=i, explanation="B",
            )
            Choice.objects.create(question=q, text="r", is_correct=True)
            Choice.objects.create(question=q, text="w", is_correct=False)

        self.assertEqual(quiz.negative_marks_per_wrong, Decimal("0"))
        self.assertIsNone(quiz.max_attempts)
        self.assertFalse(quiz.shuffle_questions)
        self.assertEqual(quiz.reveal_answers, Quiz.REVEAL_AFTER_EACH)
        self.assertEqual(list(quiz.sections.all()), [])
        self.assertFalse(quiz.questions.filter(section__isnull=False).exists())

        questions = list(quiz.questions.order_by("order"))
        attempt = self._submit(quiz, [
            (questions[0], questions[0].choices.get(is_correct=True)),
            (questions[1], questions[1].choices.get(is_correct=False)),
        ])
        self.assertEqual(attempt.score, 1.0)
        # And the student payload still renders (sections is simply []).
        r = self._student().get(f"/api/quizzes/{quiz.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["sections"], [])


class NegativeScoreDownstreamTest(_Phase4Base):
    """Negative marking (above) is intentionally allowed to push a mock
    total below zero. This class covers the readers of `attempt.score`
    that were written assuming score >= 0: the mock-average stat, the
    analytics histogram, and total_marks=0 as a division hazard negatives
    make more visible."""

    def test_avg_mock_score_reports_negative_not_floored_at_zero(self):
        """StudentQuizStatsView.avg_mock_score used to seed each quiz's
        running best at 0 (`best_by_quiz.get(quiz_id, 0)`), so a quiz whose
        only attempt scored -100% still reported a "best" of 0 — `max(-100,
        0)` always wins for 0. The running best must start at -inf so a
        genuinely negative best survives."""
        quiz = self._quiz(negative="1", questions=4)
        q = self.qs
        self._submit(quiz, [
            (q[0], q[0].wrong), (q[1], q[1].wrong),
            (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        r = self._student().get("/api/student/quizzes/stats/")
        self.assertEqual(r.status_code, 200, r.content)
        # 4 wrong × 1 mark penalty − 0 correct = -4, over total_marks 4 = -100%.
        self.assertEqual(r.data["avg_mock_score"], -100.0)

    def test_negative_attempt_lands_in_the_below_zero_bucket(self):
        """TeacherQuizAnalyticsView's score_distribution buckets used to
        start at 0, so a negative attempt fell into no bucket at all and
        silently vanished from the chart. The new "Below 0" bucket must
        catch it, prepended ahead of the pre-existing buckets whose own
        labels/boundaries must stay exactly as they were."""
        quiz = self._quiz(negative="1", questions=4)
        q = self.qs
        self._submit(quiz, [
            (q[0], q[0].wrong), (q[1], q[1].wrong),
            (q[2], q[2].wrong), (q[3], q[3].wrong),
        ])
        r = self._teacher().get(f"/api/teacher/quizzes/{quiz.id}/analytics/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["score_distribution"], [
            {"range": "Below 0", "count": 1},
            {"range": "0–19", "count": 0},
            {"range": "20–39", "count": 0},
            {"range": "40–59", "count": 0},
            {"range": "60–79", "count": 0},
            {"range": "80–100", "count": 0},
        ])

    def test_the_below_zero_bucket_is_absent_when_nothing_scored_negative(self):
        """The bucket is conditional, not unconditional. A mock with negative
        marking switched ON but no actually-negative attempt must NOT grow a
        "Below 0" column — otherwise every chart on the platform gains a
        permanently-empty bar, since negative marking is mock-only and most
        attempts are not negative. Pairs with the test above: together they
        pin both directions of the condition."""
        quiz = self._quiz(negative="0.25", questions=4)
        q = self.qs
        self._submit(quiz, [
            (q[0], q[0].right), (q[1], q[1].right),
            (q[2], q[2].right), (q[3], q[3].right),
        ])
        r = self._teacher().get(f"/api/teacher/quizzes/{quiz.id}/analytics/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn(
            "Below 0", [b["range"] for b in r.data["score_distribution"]]
        )
        self.assertEqual(len(r.data["score_distribution"]), 5)

    def test_zero_total_marks_quiz_does_not_raise_zero_division(self):
        """total_marks can fall out of sync with a quiz's questions (a
        pre-existing hazard — see QuizDashboardSerializer.get_best_score);
        at total_marks=0 every score/total_marks% site must skip the
        division rather than raise, negative score or not."""
        quiz = self._quiz(negative="0.25", questions=2)
        quiz.total_marks = 0
        quiz.save(update_fields=["total_marks"])
        q = self.qs
        attempt = self._submit(
            quiz, [(q[0], q[0].right), (q[1], q[1].wrong)]
        )
        self.assertEqual(attempt.score, 0.75)

        r = self._teacher().get(f"/api/teacher/quizzes/{quiz.id}/analytics/")
        self.assertEqual(r.status_code, 200, r.content)
        # No "Below 0" entry: it is emitted only when something actually
        # scored below zero, so a practice quiz — which can never have
        # negative marking — never grows a permanently-empty column.
        self.assertEqual(r.data["score_distribution"], [
            {"range": "0–19", "count": 0},
            {"range": "20–39", "count": 0},
            {"range": "40–59", "count": 0},
            {"range": "60–79", "count": 0},
            {"range": "80–100", "count": 0},
        ])

        r2 = self._student().get(f"/api/quizzes/{quiz.id}/result/")
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(r2.data["score"], 0.75)

        r3 = self._student().get("/api/student/quizzes/stats/")
        self.assertEqual(r3.status_code, 200, r3.content)
        self.assertEqual(r3.data["avg_mock_score"], 0)


class MockAttemptQuotaTest(_Phase4Base):
    """`max_attempts` (quota) vs the pre-existing `new_attempt` flag (intent).
    They must compose, not compete."""

    def test_max_attempts_one_blocks_a_second_mock_attempt(self):
        quiz = self._quiz(negative="0.25", questions=2, max_attempts=1)
        q = self.qs
        self._submit(quiz, [(q[0], q[0].right)])

        c = self._student()
        # Even an EXPLICIT retake request is refused — a deliberate ask
        # cannot manufacture an entitlement.
        again = c.post(
            f"/api/quizzes/{quiz.id}/start/", {"new_attempt": True}, format="json",
        )
        self.assertEqual(again.status_code, 200, again.content)
        self.assertTrue(again.data["already_submitted"])
        self.assertTrue(again.data["attempts_exhausted"])
        self.assertEqual(
            QuizAttempt.objects.filter(quiz=quiz, learner_profile=self.profile).count(), 1,
        )

    def test_null_max_attempts_allows_unlimited_practice_retries(self):
        quiz = self._quiz(quiz_type=Quiz.TYPE_PRACTICE, questions=2, max_attempts=None)
        q = self.qs
        self._submit(quiz, [(q[0], q[0].right)])
        self._submit(quiz, [(q[0], q[0].right)], new_attempt=True)
        self._submit(quiz, [(q[0], q[0].right)], new_attempt=True)
        self.assertEqual(
            QuizAttempt.objects.filter(quiz=quiz, learner_profile=self.profile).count(), 3,
        )

    def test_the_existing_new_attempt_retake_path_still_works(self):
        """The accidental-retake guard is unchanged where there is no quota:
        a bare re-post creates nothing, `new_attempt: true` starts a retake,
        and neither response claims the attempts are exhausted."""
        quiz = self._quiz(questions=2, max_attempts=None)
        q = self.qs
        self._submit(quiz, [(q[0], q[0].right)])

        c = self._student()
        bare = c.post(f"/api/quizzes/{quiz.id}/start/")
        self.assertTrue(bare.data["already_submitted"])
        self.assertNotIn("attempts_exhausted", bare.data)
        self.assertEqual(QuizAttempt.objects.filter(quiz=quiz).count(), 1)

        retake = c.post(
            f"/api/quizzes/{quiz.id}/start/", {"new_attempt": True}, format="json",
        )
        self.assertFalse(retake.data.get("already_submitted", False))
        self.assertEqual(QuizAttempt.objects.filter(quiz=quiz).count(), 2)

    def test_an_in_progress_single_attempt_mock_can_still_be_resumed(self):
        """The quota bounds how many attempts a learner gets, not whether
        they may finish the one they are in — a page refresh mid-paper must
        not lock them out of their own live attempt."""
        quiz = self._quiz(questions=2, max_attempts=1)
        c = self._student()
        first = c.post(f"/api/quizzes/{quiz.id}/start/")
        resumed = c.post(f"/api/quizzes/{quiz.id}/start/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        self.assertEqual(resumed.data["attempt_id"], first.data["attempt_id"])
        self.assertNotIn("attempts_exhausted", resumed.data)

    def test_creating_a_mock_defaults_to_one_attempt_and_reveal_after_submit(self):
        """The per-quiz-type defaults live at CREATION time (a column cannot
        hold two defaults, and Quiz.save() would re-impose them on every
        edit)."""
        r = self._teacher().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "batch_id": str(self.batch.id),
            "title": "Unit test mock", "quiz_type": Quiz.TYPE_MOCK,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        quiz = Quiz.objects.get(id=r.data["id"])
        self.assertEqual(quiz.max_attempts, 1)
        self.assertEqual(quiz.reveal_answers, Quiz.REVEAL_AFTER_SUBMIT)
        self.assertEqual(quiz.negative_marks_per_wrong, Decimal("0"))

    def test_creating_a_practice_quiz_stays_unlimited_and_reveals_after_each(self):
        r = self._teacher().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "batch_id": str(self.batch.id),
            "title": "Unit test practice", "quiz_type": Quiz.TYPE_PRACTICE,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        quiz = Quiz.objects.get(id=r.data["id"])
        self.assertIsNone(quiz.max_attempts)
        self.assertEqual(quiz.reveal_answers, Quiz.REVEAL_AFTER_EACH)

    def test_an_explicit_null_max_attempts_on_a_mock_is_respected(self):
        """The creation-time default only fills an ABSENT key — a teacher who
        deliberately opens a mock up to unlimited retries keeps that."""
        r = self._teacher().post("/api/teacher/quizzes/", {
            "subject": str(self.subject.id), "batch_id": str(self.batch.id),
            "title": "Open mock", "quiz_type": Quiz.TYPE_MOCK,
            "max_attempts": None,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(Quiz.objects.get(id=r.data["id"]).max_attempts)


class QuizSectionReplaceSemanticsTest(_Phase4Base):
    """PUT /teacher/quizzes/:pk/sections/ matches by id. The trap it exists
    to avoid: delete-all-and-recreate would SET_NULL every question.section
    and silently flatten a paper the teacher was only renaming."""

    def _put(self, quiz, sections, *, user=None):
        return self._teacher(user).put(
            f"/api/teacher/quizzes/{quiz.id}/sections/",
            {"sections": sections}, format="json",
        )

    def _two_sections(self, quiz):
        r = self._put(quiz, [
            {"name": "Section A · Objective", "order": 0, "instructions": "Tick one"},
            {"name": "Section B · Numerical", "order": 1},
        ])
        self.assertEqual(r.status_code, 200, r.content)
        a, b = QuizSection.objects.filter(quiz=quiz).order_by("order")
        # Two questions in A, one in B, one left flat.
        Question.objects.filter(id=self.qs[0].id).update(section=a)
        Question.objects.filter(id=self.qs[1].id).update(section=a)
        Question.objects.filter(id=self.qs[2].id).update(section=b)
        return a, b

    def test_renaming_a_section_preserves_its_questions(self):
        quiz = self._quiz(questions=4)
        a, b = self._two_sections(quiz)

        r = self._put(quiz, [
            {"id": str(a.id), "name": "Section A · MCQ", "order": 0},
            {"id": str(b.id), "name": "Section B · Numerical", "order": 1},
        ])
        self.assertEqual(r.status_code, 200, r.content)

        a.refresh_from_db()
        self.assertEqual(a.name, "Section A · MCQ")
        self.assertEqual(
            set(a.questions.values_list("id", flat=True)),
            {self.qs[0].id, self.qs[1].id},
        )
        self.assertEqual(b.questions.count(), 1)
        # No section was churned — same rows, same ids.
        self.assertEqual(QuizSection.objects.filter(quiz=quiz).count(), 2)

    def test_deleting_a_section_flattens_its_questions_instead_of_deleting_them(self):
        quiz = self._quiz(questions=4)
        a, b = self._two_sections(quiz)

        r = self._put(quiz, [{"id": str(a.id), "name": "Section A · Objective"}])
        self.assertEqual(r.status_code, 200, r.content)

        self.assertFalse(QuizSection.objects.filter(id=b.id).exists())
        # B's question survived and merged into the flat list.
        self.assertEqual(quiz.questions.count(), 4)
        self.qs[2].refresh_from_db()
        self.assertIsNone(self.qs[2].section_id)
        # A's grouping is untouched.
        self.assertEqual(a.questions.count(), 2)

    def test_section_ordering_is_respected(self):
        quiz = self._quiz(questions=2)
        r = self._put(quiz, [
            {"name": "Third", "order": 30},
            {"name": "First", "order": 10},
            {"name": "Second", "order": 20},
        ])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["name"] for s in r.data], ["First", "Second", "Third"])
        self.assertEqual(
            [s.name for s in QuizSection.objects.filter(quiz=quiz)],
            ["First", "Second", "Third"],
        )

    def test_order_defaults_to_payload_position(self):
        quiz = self._quiz(questions=1)
        r = self._put(quiz, [{"name": "One"}, {"name": "Two"}, {"name": "Three"}])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["name"] for s in r.data], ["One", "Two", "Three"])
        self.assertEqual([s["order"] for s in r.data], [0, 1, 2])

    def test_a_section_id_from_another_quiz_is_rejected_not_silently_created(self):
        quiz = self._quiz(questions=1)
        other = self._quiz(questions=1)
        stray = QuizSection.objects.create(quiz=other, name="Elsewhere", order=0)

        r = self._put(quiz, [{"id": str(stray.id), "name": "Mine now"}])
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(QuizSection.objects.filter(quiz=quiz).count(), 0)
        stray.refresh_from_db()
        self.assertEqual(stray.name, "Elsewhere")

    def test_a_nameless_section_is_rejected(self):
        quiz = self._quiz(questions=1)
        self.assertEqual(self._put(quiz, [{"name": "   "}]).status_code, 400)
        self.assertEqual(QuizSection.objects.filter(quiz=quiz).count(), 0)

    def test_only_the_owning_teacher_may_replace_the_sections(self):
        quiz = self._quiz(questions=1)
        r = self._put(quiz, [{"name": "Hostile"}], user=self.other_teacher)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(QuizSection.objects.filter(quiz=quiz).count(), 0)

    def test_bulk_question_save_without_a_section_key_keeps_the_grouping(self):
        """The orphaning trap from the other side: the pre-Phase-5 builder
        does not send `section`, so a save must not strip the grouping."""
        quiz = self._quiz(questions=4)
        a, _b = self._two_sections(quiz)

        payload = [
            {"id": str(q.id), "text": q.text, "marks": 1, "order": i,
             "explanation": "Because",
             "choices": [{"text": "right", "is_correct": True},
                         {"text": "wrong", "is_correct": False}]}
            for i, q in enumerate(self.qs)
        ]
        r = self._teacher().put(
            f"/api/teacher/quizzes/{quiz.id}/questions/bulk/",
            {"questions": payload}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(a.questions.count(), 2)

    def test_bulk_question_save_can_file_a_question_into_a_section(self):
        quiz = self._quiz(questions=4)
        a, _b = self._two_sections(quiz)

        target = self.qs[3]  # currently flat
        r = self._teacher().put(
            f"/api/teacher/quizzes/{quiz.id}/questions/bulk/",
            {"questions": [
                {"id": str(q.id), "text": q.text, "marks": 1, "order": i,
                 "explanation": "Because",
                 "section": str(a.id) if q.id == target.id else None,
                 "choices": [{"text": "right", "is_correct": True},
                             {"text": "wrong", "is_correct": False}]}
                for i, q in enumerate(self.qs)
            ]}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        target.refresh_from_db()
        self.assertEqual(target.section_id, a.id)
        # An explicit null ungrouped the rest.
        self.assertEqual(a.questions.count(), 1)
