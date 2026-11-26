"""Regression cover for the per-subject stats bug fixed alongside
``courses/batch_progress_views.py``'s new ``_attach_per_subject_stats``.

Before the fix, ``build_progress_stats``'s ``live_seconds`` query filtered
``LiveSessionAttendance`` on ``session__course=course`` — the WHOLE course's
subjects — regardless of the ``subjects_qs`` a caller actually passed in. So
a caller asking for one subject's stats (as ``_attach_per_subject_stats``
now does, once per entry in ``MyBatchProgressView``'s per-subject breakdown)
got the whole course's live-session attendance leaked onto every subject.

These tests build one course with two subjects that each have their own
quiz attempt, and give subject A (only) an assignment submission and live
attendance subject B has none of. Calling ``build_progress_stats`` scoped to
a single subject must report ONLY that subject's numbers, never the whole
course's totals duplicated onto both. A third test calls it with both
subjects (exactly what ``MyBatchProgressView``'s own course-wide ``stats``
block does) and asserts the combined quiz average is still the average
across both attempts — the no-regression case for the existing caller.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from assignments.models import Assignment, AssignmentSubmission
from livestream.models import LiveSession, LiveSessionAttendance
from quizzes.models import Choice, Question, Quiz, QuizAttempt, StudentAnswer

from .models import Course, Subject
from .progress_stats import build_progress_stats


class PerSubjectProgressStatsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.student = User.objects.create_user(
            username="progress_student",
            email="progress_student@example.com",
            password="pw",
        )

        cls.course = Course.objects.create(title="Per-Subject Stats Course")
        cls.subject_a = Subject.objects.create(course=cls.course, name="Physics")
        cls.subject_b = Subject.objects.create(course=cls.course, name="Chemistry")

        now = timezone.now()

        # ---- Quiz on subject A: scored 80%.
        cls.quiz_a = Quiz.objects.create(
            subject=cls.subject_a, title="Quiz A", total_marks=100,
        )
        question_a = Question.objects.create(
            quiz=cls.quiz_a, text="Q1", marks=100, order=0,
        )
        choice_a = Choice.objects.create(question=question_a, text="Right", is_correct=True)
        cls.attempt_a = QuizAttempt.objects.create(
            quiz=cls.quiz_a, student=cls.student, attempt_number=1, score=80,
            status=QuizAttempt.STATUS_SUBMITTED, submitted_at=now,
        )
        StudentAnswer.objects.create(
            attempt=cls.attempt_a, question=question_a,
            selected_choice=choice_a, is_correct=True,
        )

        # ---- Quiz on subject B: scored 40%.
        cls.quiz_b = Quiz.objects.create(
            subject=cls.subject_b, title="Quiz B", total_marks=100,
        )
        question_b = Question.objects.create(
            quiz=cls.quiz_b, text="Q1", marks=100, order=0,
        )
        choice_b = Choice.objects.create(question=question_b, text="Right", is_correct=True)
        cls.attempt_b = QuizAttempt.objects.create(
            quiz=cls.quiz_b, student=cls.student, attempt_number=1, score=40,
            status=QuizAttempt.STATUS_SUBMITTED, submitted_at=now,
        )
        StudentAnswer.objects.create(
            attempt=cls.attempt_b, question=question_b,
            selected_choice=choice_b, is_correct=False,
        )

        # ---- Assignment submission on subject A ONLY.
        cls.assignment_a = Assignment.objects.create(
            subject=cls.subject_a, title="Homework A",
            due_date=now + timedelta(days=7),
        )
        AssignmentSubmission.objects.create(
            assignment=cls.assignment_a, student=cls.student,
            submitted_at=now,
        )

        # ---- Live session + attendance on subject A ONLY.
        cls.live_session_a = LiveSession.objects.create(
            course=cls.course, subject=cls.subject_a, title="Live Class A",
            start_time=now, end_time=now + timedelta(hours=1),
            room_name="per-subject-stats-room-a",
        )
        LiveSessionAttendance.objects.create(
            session=cls.live_session_a, user=cls.student, total_seconds=1800,
        )

    def test_subject_a_gets_its_own_scoped_stats(self):
        stats = build_progress_stats(
            self.course, self.student, Subject.objects.filter(id=self.subject_a.id),
        )
        self.assertEqual(stats["quiz_avg_pct"], 80)
        self.assertEqual(stats["quizzes_completed"], 1)
        self.assertEqual(stats["assignments_done"], 1)
        self.assertGreater(stats["live_minutes"], 0)
        self.assertEqual(stats["live_minutes"], 30)

    def test_subject_b_does_not_inherit_subject_as_activity(self):
        stats = build_progress_stats(
            self.course, self.student, Subject.objects.filter(id=self.subject_b.id),
        )
        self.assertEqual(stats["quiz_avg_pct"], 40)
        self.assertEqual(stats["quizzes_completed"], 1)
        # Subject B has no assignment submission or live attendance of its
        # own — before the live_seconds fix, the whole COURSE's attendance
        # (all of it on subject A) would have leaked in here too.
        self.assertEqual(stats["assignments_done"], 0)
        self.assertEqual(stats["live_minutes"], 0)

    def test_whole_course_scope_still_averages_across_subjects(self):
        """No-regression case: MyBatchProgressView's own course-wide `stats`
        block passes `course.subjects.all()` — this must keep behaving
        exactly as it always did, blending both subjects' attempts."""
        stats = build_progress_stats(
            self.course, self.student, Subject.objects.filter(course=self.course),
        )
        self.assertEqual(stats["quiz_avg_pct"], 60)  # average of 80 and 40
        self.assertEqual(stats["quizzes_completed"], 2)
        self.assertEqual(stats["assignments_done"], 1)
        self.assertEqual(stats["live_minutes"], 30)
