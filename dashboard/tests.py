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
from courses.models import Course, Subject, Chapter, Batch
from enrollments.models import Enrollment, Subscription
from quizzes.models import Quiz


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
            is_published=True, batch=cls.morning,
        )
        cls.q_other = Quiz.objects.create(
            subject=cls.subject, created_by=cls.teacher, title="Evening quiz",
            is_published=True, batch=cls.evening,
        )

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
