"""Regression cover for the learner dashboard's coursework widgets.

Both bugs here were reported from production and were invisible to the
Assignments tab, which goes through CourseAssignmentsView and has always
been correct — the dashboard reimplemented the same query without the two
filters that make it safe:

  · BATCH ISOLATION. A subject with a Morning and an Evening batch showed
    BOTH batches' assignments on the dashboard.
  · ALREADY-SUBMITTED WORK. Nothing consulted AssignmentSubmission, so an
    assignment the learner had turned in stayed on the dashboard forever.

Submission state is keyed on learner_profile, never the account, so the
sibling case is pinned too: two children on one parent email must not clear
each other's dashboards.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, LearnerProfile
from assignments.models import Assignment, AssignmentSubmission
from courses.models import Course, Subject, Chapter, Batch, TeachingAssignment
from enrollments.models import Enrollment, Subscription
from quizzes.models import Quiz
from django.core.files.uploadedfile import SimpleUploadedFile
from dashboard.views import (
    _teacher_grading_queue, _teacher_grading_count, _teacher_group_sessions,
)
from dashboard.serializers import DashboardGroupSessionSerializer
from sessions_app.models import GroupSession


class LearnerDashboardScopingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="dash_t", email="dash_t@test.com", password="x",
        )
        cls.account = User.objects.create_user(
            username="dash_s", email="dash_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.account, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        # Two children on ONE account — the sibling-isolation case.
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="Nil", is_default=True,
        )
        cls.sibling = LearnerProfile.objects.create(
            account=cls.account, display_name="Sib",
        )

        now = timezone.now()
        cls.course = Course.objects.create(title="Class 7 CBSE")
        cls.subject = Subject.objects.create(course=cls.course, name="Mathematics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="chapter 1")
        cls.morning = Batch.objects.create(course=cls.course, name="Morning 2026", code="M26")
        cls.evening = Batch.objects.create(course=cls.course, name="Evening 2026", code="E26")

        for prof in (cls.profile, cls.sibling):
            Enrollment.objects.create(
                user=cls.account, learner_profile=prof, course=cls.course,
                batch=cls.morning, status=Enrollment.STATUS_ACTIVE,
            )
            Subscription.objects.create(
                user=cls.account, learner_profile=prof, course=cls.course,
                status=Subscription.STATUS_ACTIVE,
                starts_at=now, expires_at=now + timedelta(days=30),
            )

        due = now + timedelta(days=7)
        cls.a_mine = Assignment.objects.create(
            chapter=cls.chapter, title="Morning homework",
            batch=cls.morning, due_date=due,
        )
        cls.a_other = Assignment.objects.create(
            chapter=cls.chapter, title="Evening homework",
            batch=cls.evening, due_date=due,
        )
        cls.a_all = Assignment.objects.create(
            chapter=cls.chapter, title="Course-wide homework",
            batch=None, due_date=due,
        )

        cls.q_mine = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Morning quiz",
            is_assigned=True,
        )
        # Scope via the M2M; the `batch` shim went in Phase 10 and empty means
        # every batch, which would make the scoping assertion meaningless.
        cls.q_mine.batches.set([cls.morning])
        cls.q_other = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Evening quiz",
            is_assigned=True,
        )
        cls.q_other.batches.set([cls.evening])

    def _dashboard(self, profile):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        r = c.get("/api/dashboard/")
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def _assignment_titles(self, profile):
        return {a["title"] for a in self._dashboard(profile)["assignments"]}

    def test_other_batches_assignments_are_not_shown(self):
        titles = self._assignment_titles(self.profile)
        self.assertIn("Morning homework", titles)
        self.assertIn("Course-wide homework", titles)   # batch NULL = everyone
        self.assertNotIn("Evening homework", titles)

    def test_other_batches_quizzes_are_not_shown(self):
        titles = {q["title"] for q in self._dashboard(self.profile)["quizzes"]}
        self.assertIn("Morning quiz", titles)
        self.assertNotIn("Evening quiz", titles)

    def test_submitted_assignment_drops_off_the_dashboard(self):
        self.assertIn("Morning homework", self._assignment_titles(self.profile))
        AssignmentSubmission.objects.create(
            assignment=self.a_mine, student=self.account, learner_profile=self.profile,
        )
        self.assertNotIn("Morning homework", self._assignment_titles(self.profile))

    def test_one_siblings_submission_does_not_clear_the_others_dashboard(self):
        AssignmentSubmission.objects.create(
            assignment=self.a_mine, student=self.account, learner_profile=self.profile,
        )
        # Same ACCOUNT, different child — must still owe the work.
        self.assertIn("Morning homework", self._assignment_titles(self.sibling))

    def test_unplaced_learner_still_sees_batch_scoped_work(self):
        # Deliberate over-share, matching assignments/views.py: we cannot tell
        # which cohort an unplaced learner belongs to, so hiding batch-scoped
        # work would make it vanish with no notification.
        Enrollment.objects.filter(learner_profile=self.sibling).update(batch=None)
        titles = self._assignment_titles(self.sibling)
        self.assertIn("Morning homework", titles)
        self.assertIn("Evening homework", titles)
        self.assertIn("Course-wide homework", titles)


class AssignmentDraftGateTest(TestCase):
    """Drafts are invisible to students and silent; publishing notifies once.

    Before `Assignment.is_published`, activity/signals.assignment_created
    fired on the post_save of a brand-new row, so the class was notified the
    instant a teacher hit save — there was no way to stage an assignment.
    Quizzes already worked this way; assignments did not.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="dr_s", email="dr_s@test.com", password="x", is_verified=True,
        )
        UserRole.objects.create(
            user=cls.account, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="D", is_default=True,
        )
        now = timezone.now()
        cls.course = Course.objects.create(title="Class 8")
        cls.subject = Subject.objects.create(course=cls.course, name="Science")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="ch1")
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Enrollment.STATUS_ACTIVE,
        )
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=now, expires_at=now + timedelta(days=30),
        )

    def _client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def _titles(self):
        r = self._client().get(f"/api/assignments/courses/{self.course.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        return {a["title"] for a in r.data}

    def test_existing_assignments_default_to_published(self):
        # The migration defaults to True on purpose — a default of False
        # would have made every existing assignment vanish for students.
        a = Assignment.objects.create(
            chapter=self.chapter, title="Legacy work",
            due_date=timezone.now() + timedelta(days=3),
        )
        self.assertTrue(a.is_published)
        self.assertIn("Legacy work", self._titles())

    def test_a_draft_is_hidden_from_students_and_notifies_nobody(self):
        from activity.models import Activity
        Assignment.objects.create(
            chapter=self.chapter, title="Draft work", is_published=False,
            due_date=timezone.now() + timedelta(days=3),
        )
        self.assertNotIn("Draft work", self._titles())
        self.assertFalse(
            Activity.objects.filter(title__icontains="Draft work").exists()
        )

    def test_publishing_a_draft_reveals_it_and_notifies_once(self):
        from activity.models import Activity
        a = Assignment.objects.create(
            chapter=self.chapter, title="Staged work", is_published=False,
            due_date=timezone.now() + timedelta(days=3),
        )
        self.assertEqual(Activity.objects.filter(title__icontains="Staged work").count(), 0)

        a.is_published = True
        a.save()
        self.assertIn("Staged work", self._titles())
        self.assertEqual(Activity.objects.filter(title__icontains="Staged work").count(), 1)

        # Re-saving a live assignment (fixing a typo) must not notify again.
        a.title = "Staged work v2"
        a.save()
        self.assertEqual(Activity.objects.filter(title__icontains="Staged work").count(), 1)


class SubmissionNotifiesTheRightTeachersTest(TestCase):
    """A batch-scoped teacher must hear about their own students' submissions.

    `assignment_submitted` filtered teaching assignments on
    `batch__isnull=True`, which excluded every batch-scoped teacher — the
    people most likely to own the submission. It also wrote an Activity row
    only, with no durable Notification, so an offline teacher got no push
    and nothing in the Communication Center.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="ch1")
        cls.morning = Batch.objects.create(course=cls.course, name="Morning", code="M9")
        cls.evening = Batch.objects.create(course=cls.course, name="Evening", code="E9")

        def teacher(username):
            u = User.objects.create_user(username=username, email=f"{username}@t.com", password="x")
            UserRole.objects.create(
                user=u, role=Role.objects.get(name="TEACHER"),
                is_active=True, is_primary=True,
            )
            return u

        cls.t_course_wide = teacher("t_wide")
        cls.t_morning = teacher("t_morning")
        cls.t_evening = teacher("t_evening")

        from courses.models import TeachingAssignment
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.t_course_wide, batch=None, is_active=True)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.t_morning, batch=cls.morning, is_active=True)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.t_evening, batch=cls.evening, is_active=True)

        cls.account = User.objects.create_user(
            username="sub_s", email="sub_s@test.com", password="x", is_verified=True,
        )
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="S", is_default=True,
        )
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            batch=cls.morning, status=Enrollment.STATUS_ACTIVE,
        )
        cls.assignment = Assignment.objects.create(
            chapter=cls.chapter, title="Lab report",
            due_date=timezone.now() + timedelta(days=3),
        )

    def _notified_teachers(self):
        from activity.models import Activity
        return set(
            Activity.objects.filter(
                audience=Activity.AUDIENCE_TEACHER,
                title__icontains="Lab report",
            ).values_list("user_id", flat=True)
        )

    def test_the_students_own_batch_teacher_is_notified(self):
        AssignmentSubmission.objects.create(
            assignment=self.assignment, student=self.account,
            learner_profile=self.profile,
        )
        notified = self._notified_teachers()
        self.assertIn(self.t_morning.id, notified)      # was silently excluded
        self.assertIn(self.t_course_wide.id, notified)  # batch IS NULL
        self.assertNotIn(self.t_evening.id, notified)   # not this cohort

    def test_a_durable_notification_is_written_not_just_a_feed_row(self):
        from notifications.models import Notification
        AssignmentSubmission.objects.create(
            assignment=self.assignment, student=self.account,
            learner_profile=self.profile,
        )
        rows = Notification.objects.filter(verb="assignment.submitted")
        self.assertTrue(rows.exists())
        self.assertIn(self.t_morning.id, set(rows.values_list("recipient_id", flat=True)))
        # And it deep-links to the submissions screen, not a bare list.
        self.assertTrue(
            rows.first().link_url.endswith(f"/assignments/{self.assignment.id}/submissions")
        )


class GradingQueueBatchScopeTest(TestCase):
    """The grading queue must list only work the teacher can actually grade.

    _ungraded_submissions_q was SUBJECT-level while the per-object gate
    (_assert_teacher_owns_assignment) is batch-aware, so a teacher staffed on
    one batch saw another batch's submissions, the "N pending" badge counted
    them, and every Grade button on those rows 403'd.

    These tests assert the queue agrees with the GATE, not with some number —
    a queue that disagrees with the thing it links to is the actual defect.
    """

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Algebra", order=0)
        cls.batch_a = Batch.objects.create(course=cls.course, name="10-A", code="A")
        cls.batch_b = Batch.objects.create(course=cls.course, name="10-B", code="B")

        # Staffed on 10-B ONLY.
        cls.teacher_b = User.objects.create_user(
            username="tb", email="tb@t.com", password="x", is_verified=True)
        UserRole.objects.create(
            user=cls.teacher_b, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            batch=cls.batch_b, subject=cls.subject, teacher=cls.teacher_b,
            is_active=True)

        due = timezone.now() + timedelta(days=3)
        cls.a_10a = Assignment.objects.create(
            chapter=cls.chapter, batch=cls.batch_a, title="10-A work",
            due_date=due, is_published=True)
        cls.a_10b = Assignment.objects.create(
            chapter=cls.chapter, batch=cls.batch_b, title="10-B work",
            due_date=due, is_published=True)
        cls.a_wide = Assignment.objects.create(
            chapter=cls.chapter, batch=None, title="course-wide work",
            due_date=due, is_published=True)

        for a in (cls.a_10a, cls.a_10b, cls.a_wide):
            acct = User.objects.create_user(
                username=f"kid-{a.title}", email=f"{a.id}@t.com",
                password="x", is_verified=True)
            prof = LearnerProfile.objects.create(
                account=acct, display_name="kid", is_default=True)
            AssignmentSubmission.objects.create(
                assignment=a, student=acct, learner_profile=prof,
                submitted_file=SimpleUploadedFile("s.pdf", b"%PDF"))

    def _titles(self):
        return {s.assignment.title for s in _teacher_grading_queue(self.teacher_b)}

    def test_another_batchs_submission_is_not_queued(self):
        """The bug: a 10-B teacher saw 10-A's work and got a 403 on Grade."""
        self.assertNotIn("10-A work", self._titles())

    def test_own_batch_is_queued(self):
        self.assertIn("10-B work", self._titles())

    def test_course_wide_assignment_is_queued_for_a_batch_only_teacher(self):
        """The asymmetry. A course-wide assignment gates on teaches_subject(),
        which ANY active row satisfies — so a 10-B-only teacher MAY grade it.
        Collapsing the two gate branches into one would wrongly drop this."""
        self.assertIn("course-wide work", self._titles())

    def test_count_matches_the_queue(self):
        self.assertEqual(_teacher_grading_count(self.teacher_b), len(self._titles()))

    def test_count_is_not_inflated_by_double_staffing(self):
        """171 of 172 staffed subjects on prod carry BOTH a course-wide and a
        batch-scoped row. A join would multiply; Exists cannot."""
        TeachingAssignment.objects.create(
            batch=None, subject=self.subject, teacher=self.teacher_b,
            is_active=True)
        self.assertEqual(_teacher_grading_count(self.teacher_b), 3)
        self.assertEqual(len(_teacher_grading_queue(self.teacher_b)), 3)

    def test_queue_agrees_with_the_per_object_gate(self):
        """The property that actually matters: everything listed must pass the
        gate the Grade button hits, and nothing excluded may pass it."""
        from rest_framework.exceptions import PermissionDenied
        from assignments.views import _assert_teacher_owns_assignment

        queued = {s.assignment_id for s in _teacher_grading_queue(self.teacher_b)}
        for a in (self.a_10a, self.a_10b, self.a_wide):
            try:
                _assert_teacher_owns_assignment(self.teacher_b, a)
                allowed = True
            except PermissionDenied:
                allowed = False
            self.assertEqual(
                a.id in queued, allowed,
                f"{a.title}: queued={a.id in queued} but gate allows={allowed}",
            )

    def test_graded_work_leaves_the_queue(self):
        sub = AssignmentSubmission.objects.get(assignment=self.a_10b)
        sub.graded_at = timezone.now()
        sub.save(update_fields=["graded_at"])
        self.assertNotIn("10-B work", self._titles())


class TeacherDashboardGroupSessionsTest(TestCase):
    """/dashboard/ returned no `group_sessions` key at all, so the teacher's
    Today timeline silently omitted them — live sessions and private sessions
    were both there, group sessions were the only one of the three missing. A
    teacher hosting a group session at 4pm saw an empty afternoon."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.host = User.objects.create_user(
            username="gs_host", email="gshost@t.com", password="x", is_verified=True)
        cls.invited = User.objects.create_user(
            username="gs_inv", email="gsinv@t.com", password="x", is_verified=True)
        cls.stranger = User.objects.create_user(
            username="gs_other", email="gsother@t.com", password="x", is_verified=True)
        for u in (cls.host, cls.invited, cls.stranger):
            UserRole.objects.create(
                user=u, role=Role.objects.get(name="TEACHER"),
                is_active=True, is_primary=True)

        today = timezone.localtime(timezone.now()).date()
        cls.hosted = GroupSession.objects.create(
            host=cls.host, topic="Doubt clearing", short_code="GS0001", scheduled_date=today,
            scheduled_time="16:00", duration_minutes=45, status="scheduled")
        cls.as_invited = GroupSession.objects.create(
            host=cls.stranger, invited_teacher=cls.invited, topic="Guest slot", short_code="GS0002",
            scheduled_date=today, scheduled_time="18:00",
            duration_minutes=45, status="scheduled")
        cls.cancelled = GroupSession.objects.create(
            host=cls.host, topic="Called off", short_code="GS0003", scheduled_date=today,
            scheduled_time="20:00", duration_minutes=45, status="cancelled")
        cls.yesterday = GroupSession.objects.create(
            host=cls.host, topic="Last week", short_code="GS0004", scheduled_date=today - timedelta(days=2),
            scheduled_time="16:00", duration_minutes=45, status="scheduled")

    def test_host_sees_their_own_session(self):
        rows = _teacher_group_sessions(self.host, timezone.now())
        self.assertIn("Doubt clearing", {g.topic for g in rows})

    def test_invited_teacher_sees_it_too(self):
        """A teacher reaches a group session as host OR as invited teacher."""
        rows = _teacher_group_sessions(self.invited, timezone.now())
        self.assertEqual({g.topic for g in rows}, {"Guest slot"})

    def test_an_unrelated_teacher_sees_nothing(self):
        rows = _teacher_group_sessions(
            User.objects.create_user(
                username="nobody", email="nb@t.com", password="x"),
            timezone.now())
        self.assertEqual(rows, [])

    def test_cancelled_and_past_are_excluded(self):
        topics = {g.topic for g in _teacher_group_sessions(self.host, timezone.now())}
        self.assertNotIn("Called off", topics)
        self.assertNotIn("Last week", topics)

    def test_host_and_invited_on_the_same_row_is_not_duplicated(self):
        """Both Q branches can match one row. Without .distinct() the OR
        returns it twice and the timeline shows the class twice."""
        self.hosted.invited_teacher = self.host
        self.hosted.save(update_fields=["invited_teacher"])
        rows = _teacher_group_sessions(self.host, timezone.now())
        self.assertEqual(len([g for g in rows if g.topic == "Doubt clearing"]), 1)

    def test_serializer_reports_the_role(self):
        data = DashboardGroupSessionSerializer(
            [self.hosted], many=True, context={"user": self.host}).data
        self.assertEqual(data[0]["role"], "host")
        data2 = DashboardGroupSessionSerializer(
            [self.as_invited], many=True, context={"user": self.invited}).data
        self.assertEqual(data2[0]["role"], "invited_teacher")

    def test_serializer_shape_matches_the_private_session_one(self):
        """The timeline merges three session kinds; date/time must not be
        scheduled_date/scheduled_time on one of them."""
        data = DashboardGroupSessionSerializer(
            [self.hosted], many=True, context={"user": self.host}).data[0]
        self.assertIn("date", data)
        self.assertIn("time", data)
        self.assertNotIn("scheduled_date", data)
