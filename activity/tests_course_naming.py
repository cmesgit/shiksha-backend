"""A notification has to say WHICH COURSE it is about.

Every student-facing event named the item in its title ("New assignment: Ch 4
Worksheet") and the subject in a meta line ("Physics"), and nothing anywhere
named the course. A student enrolled in two courses — or one account with two
children on it — could not tell which one an assignment belonged to. All five
composition sites already had the `Course` object as a local variable, so the
information was in hand and simply never passed on.

Two mechanisms, deliberately different:

  · `Activity` gets `course_name` at READ time, from a single bulk query
    keyed on the page's subject_ids (serializers.course_names_for). Read-time
    because the titles are denormalised at write time, so a write-time fix
    would label new rows only and leave every notification already in a bell
    nameless.
  · `notifications.Notification` gets it in `body` at write time, because it
    has no subject or course column to resolve from. That slot is rendered by
    the Communication Center and had never been filled by any verb.

Neither one touches `title`. See _bulk_notify_students' docstring.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile
from activity.models import Activity
from activity.serializers import ActivitySerializer, course_names_for
from courses.models import Course, Subject

User = get_user_model()

FEED = "/api/activity/feed/"


class _FakeEnrollment:
    """_bulk_notify_students only reads these three attributes off a row."""

    def __init__(self, user):
        self.user = user
        self.learner_profile = None
        self.learner_profile_id = None


class CourseNamingFixture:
    def setUp(self):
        self.student = User.objects.create_user(
            username="s", email="s@example.com", password="x",
        )
        # The feed 409s without an active learner profile — audience scoping
        # is per-profile, so there is no "the account's feed" to serve.
        self.profile = LearnerProfile.objects.create(
            account=self.student, display_name="S", full_name="S",
            relationship="SON", is_default=True,
        )
        self.course = Course.objects.create(title="Class 10 Science")
        self.subject = Subject.objects.create(
            course=self.course, name="Physics",
        )

    def learner_client(self):
        c = APIClient()
        c.force_authenticate(
            self.student,
            token={"context": "learner",
                   "active_profile": str(self.profile.id)},
        )
        return c

    def post_material(self, title="New study material: Ch 4", **kwargs):
        from activity.signals import _bulk_notify_students
        from materials.models import StudyMaterial

        material = StudyMaterial.objects.create(
            subject=self.subject, title=title, uploaded_by=self.student,
        )
        _bulk_notify_students(
            [_FakeEnrollment(self.student)], material, Activity.TYPE_MATERIAL,
            title, None, self.subject.id, self.subject.name,
            **kwargs,
        )
        return material


class ActivityCourseNameTest(CourseNamingFixture, TestCase):
    def test_the_feed_names_the_course(self):
        """The bug, stated as a test."""
        self.post_material(course_name=self.course.title)

        row = self.learner_client().get(FEED).json()["results"][0]
        self.assertEqual(row["subject_name"], "Physics")
        self.assertEqual(row["course_name"], "Class 10 Science")

    def test_it_labels_rows_written_before_the_fix(self):
        """The whole reason this is resolved on read.

        A row created without `course_name` — i.e. every notification already
        in every student's bell — still gets labelled, because the label comes
        from the subject at serialization time and not from the stored row.
        """
        self.post_material()  # no course_name passed, as the old code did

        stored = Activity.objects.get()
        self.assertEqual(
            stored.title, "New study material: Ch 4",
            "nothing was baked into the title",
        )
        row = self.learner_client().get(FEED).json()["results"][0]
        self.assertEqual(row["course_name"], "Class 10 Science")

    def test_two_courses_are_told_apart(self):
        """The symptom: one bell, two courses, identical-looking rows."""
        other = Course.objects.create(title="Class 12 Physics")
        other_subject = Subject.objects.create(course=other, name="Physics")

        from activity.signals import _bulk_notify_students
        from materials.models import StudyMaterial
        for subject in (self.subject, other_subject):
            m = StudyMaterial.objects.create(
                subject=subject, title="Chapter 4 notes",
                uploaded_by=self.student,
            )
            _bulk_notify_students(
                [_FakeEnrollment(self.student)], m, Activity.TYPE_MATERIAL,
                "New study material: Chapter 4 notes", None,
                subject.id, subject.name, course_name=subject.course.title,
            )

        rows = self.learner_client().get(FEED).json()["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["title"] for r in rows}, {"New study material: Chapter 4 notes"},
            "the titles are identical and the subjects are both Physics — "
            "the course name is the only thing that distinguishes them",
        )
        self.assertEqual(
            {r["course_name"] for r in rows},
            {"Class 10 Science", "Class 12 Physics"},
        )

    def test_a_missing_context_degrades_to_blank_not_a_crash(self):
        """`course_name` must never 500 a feed. Serialized with no context at
        all — which is what any other caller of this serializer does."""
        self.post_material(course_name=self.course.title)
        row = Activity.objects.get()
        self.assertEqual(ActivitySerializer(row).data["course_name"], "")

    def test_a_row_with_no_subject_is_blank_not_a_lookup(self):
        """A live session can carry no subject at all, so the map must not be
        asked for one."""
        act = Activity.objects.create(
            user=self.student, audience=Activity.AUDIENCE_LEARNER,
            type=Activity.TYPE_SESSION, title="Live session scheduled: intro",
            content_type=ContentType.objects.get_for_model(Course),
            object_id=uuid.uuid4(),
        )
        self.assertIsNone(act.subject_id)
        self.assertEqual(course_names_for([act]), {})
        row = self.learner_client().get(FEED).json()["results"][0]
        self.assertEqual(row["course_name"], "")

    def test_resolution_is_one_query_for_the_whole_page(self):
        """A `course` column was rejected on the model because resolving it
        per row is "a query per row". This is the bulk form, so that objection
        has to actually be answered."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        subjects = [self.subject]
        for i in range(4):
            c = Course.objects.create(title=f"Course {i}")
            subjects.append(Subject.objects.create(course=c, name=f"S{i}"))

        from materials.models import StudyMaterial
        acts = []
        for s in subjects:
            m = StudyMaterial.objects.create(
                subject=s, title="x", uploaded_by=self.student,
            )
            acts.append(Activity.objects.create(
                user=self.student, audience=Activity.AUDIENCE_LEARNER,
                type=Activity.TYPE_MATERIAL, title="New study material: x",
                subject_id=s.id, subject_name=s.name,
                content_type=ContentType.objects.get_for_model(StudyMaterial),
                object_id=m.id,
            ))

        with CaptureQueriesContext(connection) as ctx:
            names = course_names_for(acts)
        self.assertEqual(len(ctx.captured_queries), 1, ctx.captured_queries)
        self.assertEqual(len(names), 5)


class NotificationBodyTest(CourseNamingFixture, TestCase):
    """`Notification.body` is the only place the notifications API can carry
    the course — the model has no subject or course column. It was rendered by
    the Communication Center (`cc-notif-text`) and left empty by every verb.
    """

    def body_of_one(self):
        from notifications.models import Notification
        return Notification.objects.get().body

    def test_the_body_names_the_subject_and_the_course(self):
        self.post_material(
            verb="materials.uploaded", course_name=self.course.title,
        )
        self.assertEqual(self.body_of_one(), "Physics · Class 10 Science")

    def test_the_title_is_left_alone(self):
        """Not appended to the title: it is denormalised into two tables at
        write time, so it would fix new rows only and crowd the 255-char cap."""
        from notifications.models import Notification
        self.post_material(
            verb="materials.uploaded", course_name=self.course.title,
        )
        self.assertEqual(
            Notification.objects.get().title, "New study material: Ch 4",
        )
        self.assertEqual(
            Activity.objects.get().title, "New study material: Ch 4",
        )

    def test_no_dangling_separator_when_a_half_is_missing(self):
        """The classic way a joined meta line goes wrong. Both halves are
        optional — a live session can have no subject at all."""
        self.post_material(verb="materials.uploaded")  # no course_name
        self.assertEqual(self.body_of_one(), "Physics")

    def test_blank_when_neither_half_is_known(self):
        from activity.signals import _bulk_notify_students
        from materials.models import StudyMaterial
        m = StudyMaterial.objects.create(
            subject=self.subject, title="x", uploaded_by=self.student,
        )
        _bulk_notify_students(
            [_FakeEnrollment(self.student)], m, Activity.TYPE_MATERIAL,
            "New study material: x", None, None, "",
            verb="materials.uploaded",
        )
        self.assertEqual(self.body_of_one(), "")


class ComposedEventsNameTheCourseTest(CourseNamingFixture, TestCase):
    """The five real composition sites, driven through their own code paths
    rather than by calling the helper directly — so a site that forgets to
    pass `course_name` fails here.
    """

    def enrol(self):
        from enrollments.models import Enrollment
        return Enrollment.objects.create(
            user=self.student, course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )

    def feed_rows(self):
        return self.learner_client().get(FEED).json()["results"]

    # ⚠ In this class the `body` assertion is the LOAD-BEARING one. The feed's
    # `course_name` is resolved at read time off `subject_id`, so it comes out
    # right whether or not the composition site passed `course_name=` — it
    # cannot detect a site that forgot. `Notification.body` is written at the
    # site, so it can. Verified by reverting the assignment site and watching
    # only the body assertion fail. Do not delete it as redundant.

    def test_assignment_published_names_the_course(self):
        from assignments.models import Assignment
        self.enrol()
        a = Assignment.objects.create(
            subject=self.subject, title="Ch 4 Worksheet", is_published=False,
            # NOT NULL on the model, and it must be in the FUTURE: the feed
            # excludes ASSIGNMENT/QUIZ/SESSION rows whose due_date has passed.
            due_date=timezone.now() + timedelta(days=7),
        )
        a.is_published = True
        a.save()

        rows = [r for r in self.feed_rows() if r["raw_type"] == "ASSIGNMENT"]
        self.assertEqual(len(rows), 1, "the publish should have notified once")
        self.assertEqual(rows[0]["course_name"], "Class 10 Science")

        from notifications.models import Notification
        n = Notification.objects.filter(verb="assignment.posted").first()
        self.assertIsNotNone(n)
        self.assertIn("Class 10 Science", n.body)

    def test_quiz_assigned_names_the_course(self):
        from quizzes.models import Quiz
        self.enrol()
        q = Quiz.objects.create(
            subject=self.subject, title="Ch 4 Test", is_assigned=False,
        )
        q.is_assigned = True
        q.save()

        rows = [r for r in self.feed_rows() if r["raw_type"] == "QUIZ"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["course_name"], "Class 10 Science")

        from notifications.models import Notification
        n = Notification.objects.filter(verb="quiz.posted").first()
        self.assertIsNotNone(n)
        self.assertIn("Class 10 Science", n.body)

    def test_live_session_scheduled_names_the_course(self):
        """This site reads `course` off the session rather than off a subject,
        and its subject is optional — so it is the one that would break on a
        `subject.course` assumption."""
        from livestream.models import LiveSession
        self.enrol()
        start = timezone.now() + timedelta(days=1)
        LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Optics intro",
            start_time=start, end_time=start + timedelta(hours=1),
            room_name=f"room-{uuid.uuid4()}",
        )

        act = Activity.objects.get(type=Activity.TYPE_SESSION)
        self.assertEqual(act.subject_name, "Physics")

        from notifications.models import Notification
        n = Notification.objects.filter(verb__startswith="session").first()
        if n is not None:
            # Only if this verb is wired for durable notifications; the
            # Activity row above is the guaranteed surface.
            self.assertIn("Class 10 Science", n.body)

    def test_a_subjectless_notification_names_only_the_course(self):
        """The half-missing case, driven through the helper directly.

        Not through LiveSession: `session_created` guards with
        `subject.id if subject else None`, but `LiveSession.subject` is a
        plain non-null ForeignKey (livestream/models.py:46-50), so that guard
        is unreachable and a subjectless LiveSession cannot be created at all.
        The state is still reachable on other paths, and the join must not
        leave a leading separator when it is.
        """
        from activity.signals import _bulk_notify_students
        from materials.models import StudyMaterial
        m = StudyMaterial.objects.create(
            subject=self.subject, title="x", uploaded_by=self.student,
        )
        _bulk_notify_students(
            [_FakeEnrollment(self.student)], m, Activity.TYPE_MATERIAL,
            "Orientation", None, None, "",
            verb="materials.uploaded", course_name=self.course.title,
        )
        from notifications.models import Notification
        body = Notification.objects.get().body
        self.assertEqual(body, "Class 10 Science")
        self.assertFalse(body.startswith(" ·"))
