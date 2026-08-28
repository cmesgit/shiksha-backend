# Cover for the fields GET /activity/feed/ must expose for the bell to build
# a correct deep link.
#
# The teacher bell had a real, user-visible bug here. A SUBMISSION row's
# handler read `notif.id` and used it as the assignment id:
#
#     navigate(`/teacher/classes/${subject_id}/assignments/${id}/submissions`)
#
# but `id` is the Activity row's OWN primary key (activity/models.py — a
# UUIDField default=uuid.uuid4), while the assignment id lives in `object_id`
# (set from `object_id=obj.id` in activity/signals.py's _notify_teacher).
# Both are UUIDv4, so React Router matched, Django's <uuid:...> converter
# matched, and the failure only surfaced as a 404 at the DB lookup — which the
# screen rendered as the generic "That didn't load — Couldn't load
# submissions." Reaching the same screen through the Communication Center
# worked, because that surface reads /notifications/ and follows the
# server-authored link_url instead.
#
# These tests pin the contract the bell depends on:
#   • object_id is the PARENT object, never the row id
#   • object_type discriminates an assignment submission from a quiz one
#
# object_type matters because BOTH are Activity.TYPE_SUBMISSION. The backend
# does tag quiz submissions with subtype="quiz_submission", but only via
# `extra=`, which _ws_payload merges into the ephemeral WebSocket frame and
# nothing persists — so a quiz submission read back from THIS feed (page
# load, or "See all") arrived with no discriminator and fell through to the
# assignment branch. content_type is already on every row, so object_type
# needs no migration and cannot drift from the object it describes.

import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.auth_flow import CTX_TEACHER
from assignments.models import Assignment
from quizzes.models import Quiz

from .models import Activity
from .serializers import ActivitySerializer

User = get_user_model()


class ActivityFeedRoutingContractTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rc_t", email="rc_t@example.com", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user, token={"context": CTX_TEACHER})

    def _row(self, model_cls, parent_id, title):
        return Activity.objects.create(
            user=self.user,
            audience=Activity.AUDIENCE_TEACHER,
            type=Activity.TYPE_SUBMISSION,
            title=title,
            content_type=ContentType.objects.get_for_model(model_cls),
            object_id=parent_id,
        )

    def _fetch(self, title):
        res = self.client.get("/api/activity/feed/")
        self.assertEqual(res.status_code, 200, res.content)
        rows = [r for r in res.data["results"] if r["title"] == title]
        self.assertEqual(len(rows), 1, f"expected exactly one {title!r} row")
        return rows[0]

    def test_object_id_is_the_assignment_not_the_activity_row(self):
        assignment_id = uuid.uuid4()
        row = self._row(Assignment, assignment_id, "hruaia submitted: hgk")

        served = self._fetch("hruaia submitted: hgk")
        self.assertEqual(served["object_id"], str(assignment_id))
        self.assertEqual(served["id"], str(row.id))
        self.assertNotEqual(
            served["object_id"], served["id"],
            "the bell builds its submissions URL from object_id — if these "
            "were interchangeable this test could not catch the swap",
        )

    def test_object_type_marks_an_assignment_submission(self):
        row = self._row(Assignment, uuid.uuid4(), "submitted: essay")
        self.assertEqual(self._fetch("submitted: essay")["object_type"], "assignment")

    def test_object_type_marks_a_quiz_submission(self):
        """Both are TYPE_SUBMISSION; only object_type tells them apart.

        Without this the bell sends a quiz submission to
        /assignments/<quiz_id>/submissions — a 404 for the same reason the
        original bug was a 404, just from a different wrong id.
        """
        self._row(Quiz, uuid.uuid4(), "submitted: Untitled quiz")
        self.assertEqual(
            self._fetch("submitted: Untitled quiz")["object_type"], "quiz")

    def test_link_url_round_trips_from_the_notifier_to_the_feed(self):
        """The bell prefers link_url over its own routing, so it must arrive.

        Before Activity had this column the backend computed a correct link,
        stored it only on notifications.Notification, and left the bell to
        re-derive the route client-side — which is how the bell and the
        Communication Center ended up disagreeing about the same event.
        """
        from courses.models import Course, Subject
        from materials.models import StudyMaterial
        from activity.signals import _bulk_notify_students

        course = Course.objects.create(title="Class 12 Science")
        subject = Subject.objects.create(course=course, name="Mathematics")
        material = StudyMaterial.objects.create(
            subject=subject, title="cx dsv", uploaded_by=self.user)

        class _FakeEnrollment:
            def __init__(self, user):
                self.user = user
                self.learner_profile = None
                self.learner_profile_id = None

        link = f"/study-material/list/{subject.id}?course={course.id}"
        _bulk_notify_students(
            [_FakeEnrollment(self.user)], material, Activity.TYPE_MATERIAL,
            "New study material: cx dsv", None, subject.id, subject.name,
            link_url=link,
        )

        row = Activity.objects.get(title="New study material: cx dsv")
        self.assertEqual(row.link_url, link)

        # And it survives serialization to the feed the bell actually reads.
        self.client.force_authenticate(
            self.user, token={"context": "learner",
                              "active_profile": None})
        served = ActivitySerializer(row).data
        self.assertEqual(served["link_url"], link)
        self.assertIn(
            "course=", served["link_url"],
            "the ?course= param is the whole reason this column exists — an "
            "Activity row knows its subject but not its course, so the client "
            "cannot reconstruct this link",
        )

    def test_link_url_is_blank_not_missing_on_a_row_that_has_none(self):
        """Legacy rows must fall through to type-based routing, not crash.

        Both bells guard with `if (link_url && link_url.startsWith("/"))`, so
        "" is the correct legacy value and the key must still be present.
        """
        self._row(Assignment, uuid.uuid4(), "no link here")
        served = self._fetch("no link here")
        self.assertIn("link_url", served)
        self.assertEqual(served["link_url"], "")

    def test_object_type_is_present_on_every_row(self):
        """A missing key would make the bell's `=== "quiz"` check silently
        false rather than loudly wrong — the exact failure mode `subtype` had.
        """
        self._row(Assignment, uuid.uuid4(), "a")
        self._row(Quiz, uuid.uuid4(), "b")
        res = self.client.get("/api/activity/feed/")
        for served in res.data["results"]:
            self.assertIn("object_type", served)
            self.assertTrue(served["object_type"])
