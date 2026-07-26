"""Livestream data-hardening tests.

Simulates the LiveKit webhook lifecycle (join → leave → rejoin → room_finished)
with lightweight fake event objects and asserts the durable-capture guarantees:
idempotent event log, append-only attendance intervals, reconciled left_at,
actual start/end stamps, and the rollup. Run:

    python manage.py test livestream
"""
import uuid
from datetime import timedelta
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from courses.models import Board, Course, Subject
from livestream.consumers import CourseSessionConsumer
from livestream.models import (
    LiveSession,
    LiveSessionAttendance,
    LiveSessionAttendanceInterval,
    LiveKitWebhookEvent,
)
from livestream import views as lv

User = get_user_model()


def _evt(event, room, identity=None, eid=None):
    """Fake LiveKit webhook event with the fields our handlers read."""
    return SimpleNamespace(
        event=event,
        room=SimpleNamespace(name=room),
        participant=SimpleNamespace(identity=str(identity)) if identity else None,
        id=eid or str(uuid.uuid4()),
        created_at=0,
    )


class WebhookHardeningTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username="t@x.com", email="t@x.com", password="x")
        self.student = User.objects.create_user(username="s@x.com", email="s@x.com", password="x")
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Ch1",
            start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=1),
            room_name="room_test", created_by=self.teacher,
            status=LiveSession.STATUS_SCHEDULED,
        )

    def test_join_stamps_actual_start_and_opens_interval(self):
        lv._handle_participant_join(_evt("participant_joined", "room_test", self.teacher.id))
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.actual_started_at)
        self.assertEqual(self.session.status, LiveSession.STATUS_LIVE)
        self.assertEqual(
            LiveSessionAttendanceInterval.objects.filter(
                session=self.session, user=self.teacher, left_at__isnull=True
            ).count(),
            1,
        )

    def test_rejoin_is_append_only_and_rolls_up(self):
        lv._handle_participant_join(_evt("participant_joined", "room_test", self.student.id))
        lv._handle_participant_left(_evt("participant_left", "room_test", self.student.id))
        lv._handle_participant_join(_evt("participant_joined", "room_test", self.student.id))
        lv._handle_participant_left(_evt("participant_left", "room_test", self.student.id))
        intervals = LiveSessionAttendanceInterval.objects.filter(session=self.session, user=self.student)
        self.assertEqual(intervals.count(), 2)
        self.assertTrue(all(i.left_at for i in intervals))
        rollup = LiveSessionAttendance.objects.get(session=self.session, user=self.student)
        self.assertGreaterEqual(rollup.total_seconds, 0)
        self.assertIsNotNone(rollup.left_at)

    def test_room_finished_reconciles_open_intervals_and_stamps_end(self):
        lv._handle_participant_join(_evt("participant_joined", "room_test", self.student.id))
        # student never sends participant_left — room_finished must close it
        lv._handle_room_finished(_evt("room_finished", "room_test"))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, LiveSession.STATUS_COMPLETED)
        self.assertIsNotNone(self.session.actual_ended_at)
        self.assertEqual(
            LiveSessionAttendanceInterval.objects.filter(session=self.session, left_at__isnull=True).count(),
            0,
        )

    def test_webhook_event_log_is_idempotent(self):
        LiveKitWebhookEvent.objects.get_or_create(
            event_id="fixed-id",
            defaults={"event_type": "participant_joined", "room_name": "room_test",
                      "session": self.session, "processed": True},
        )
        _, created = LiveKitWebhookEvent.objects.get_or_create(
            event_id="fixed-id",
            defaults={"event_type": "participant_joined", "room_name": "room_test"},
        )
        self.assertFalse(created)
        self.assertEqual(LiveKitWebhookEvent.objects.filter(event_id="fixed-id").count(), 1)


class AdminEndpointSmokeTests(TestCase):
    """Every new admin (is_staff) endpoint answers 200 with the expected top-level
    keys for a staff user. Guards the URL wiring + response shape for Parts C & D."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="a@x.com", email="a@x.com", password="x", is_staff=True
        )
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        course = Course.objects.create(board=board, title="C10", class_level=10)
        subject = Subject.objects.create(course=course, name="Physics")
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=course, subject=subject, title="Ch1",
            start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=1),
            room_name="room_smoke", created_by=self.admin, status=LiveSession.STATUS_LIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _get(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, f"{url} → {r.status_code}: {r.content[:200]}")
        return r.json()

    def test_livestream_admin_endpoints(self):
        self.assertIn("data", self._get("/api/livestream/admin/streams/"))
        self.assertIn("data", self._get("/api/livestream/admin/live-now/"))
        self.assertIn("data", self._get("/api/livestream/admin/recordings/"))
        detail = self._get(f"/api/livestream/admin/streams/{self.session.id}/")
        for key in ("stream", "attendance", "chat", "viewer_samples"):
            self.assertIn(key, detail)

    def test_aggregation_endpoints(self):
        self.assertIn("data", self._get("/api/courses/admin/teacher-directory/"))
        ta = self._get("/api/activity/admin/teacher-activity/?range=7d")
        self.assertIn("kpis", ta)
        self.assertIn("feed", ta)
        mo = self._get("/api/forum/admin/moderation-overview/?range=7d")
        for key in ("kpis", "moderators", "breakdown", "queues"):
            self.assertIn(key, mo)
        an = self._get("/api/dashboard/admin/analytics/?range=30d&metric=enrollments")
        for key in ("kpis", "series", "breakdowns"):
            self.assertIn(key, an)

    def test_non_staff_is_forbidden(self):
        peon = User.objects.create_user(username="p@x.com", email="p@x.com", password="x")
        client = APIClient()
        client.force_authenticate(user=peon)
        r = client.get("/api/livestream/admin/streams/")
        self.assertIn(r.status_code, (401, 403))


@override_settings(LIVEKIT_API_KEY="test-key", LIVEKIT_API_SECRET="test-secret")
class JoinLiveSessionContextTests(TestCase):
    """join_live_session must branch on the JWT `context` claim, not on
    has_role(). Regression for: a TEACHER account has no STUDENT role (see
    signup_serializer._setup_teacher), so a teacher joining in LEARNER context
    (e.g. their own SELF learner profile) must still get a STUDENT token —
    not fall through to the has_role("TEACHER") branch and get PRESENTER."""

    def setUp(self):
        from accounts.models import LearnerProfile, Role, UserRole
        from courses.models import SubjectTeacher
        from enrollments.models import Enrollment, Subscription

        self.teacher = User.objects.create_user(
            username="t@x.com", email="t@x.com", password="x"
        )
        teacher_role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(
            user=self.teacher, role=teacher_role, is_active=True, is_primary=True
        )
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        SubjectTeacher.objects.create(subject=self.subject, teacher=self.teacher)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Ch1",
            start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=1),
            room_name="room_ctx", created_by=self.teacher,
            status=LiveSession.STATUS_LIVE,
        )

        # The teacher's own SELF learner profile + an active subscription so
        # this is a clean isolate of the context-branching bug, not a
        # subscription/enrollment gap.
        self.self_profile = LearnerProfile.objects.create(
            account=self.teacher, display_name="Self", full_name="Self",
            student_id="SELF001", is_default=True,
        )
        Enrollment.objects.create(
            user=self.teacher, learner_profile=self.self_profile,
            course=self.course, status=Enrollment.STATUS_ACTIVE,
        )
        Subscription.objects.create(
            user=self.teacher, learner_profile=self.self_profile,
            course=self.course, starts_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
            status=Subscription.STATUS_ACTIVE,
        )

    def _client(self, context, profile=None):
        client = APIClient()
        token = {"context": context}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        client.force_authenticate(user=self.teacher, token=token)
        return client

    def test_teacher_in_learner_context_gets_student_role(self):
        client = self._client("learner", profile=self.self_profile)
        r = client.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["role"], "STUDENT")

    def test_teacher_in_teacher_context_gets_presenter_role(self):
        client = self._client("teacher")
        r = client.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["role"], "PRESENTER")

    def test_learner_context_without_active_profile_is_rejected(self):
        client = self._client("learner")
        r = client.post(f"/api/livestream/sessions/{self.session.id}/join/")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("lock_reason"), "no_learner_profile")

    def test_status_endpoint_reachable_by_both_sides(self):
        # Student (learner-context, enrolled) — was a dead 403 fallback before
        # live_session_status existed (classroom_screen polled the
        # teacher-only detail/ endpoint).
        learner_client = self._client("learner", profile=self.self_profile)
        r = learner_client.get(f"/api/livestream/sessions/{self.session.id}/status/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], LiveSession.STATUS_LIVE)

        teacher_client = self._client("teacher")
        r = teacher_client.get(f"/api/livestream/sessions/{self.session.id}/status/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], LiveSession.STATUS_LIVE)

    def test_status_endpoint_rejects_unenrolled_learner(self):
        from accounts.models import LearnerProfile

        outsider = User.objects.create_user(
            username="o@x.com", email="o@x.com", password="x"
        )
        profile = LearnerProfile.objects.create(
            account=outsider, display_name="Out", full_name="Out",
            student_id="OUT001", is_default=True,
        )
        client = APIClient()
        client.force_authenticate(
            user=outsider,
            token={"context": "learner", "active_profile": str(profile.id)},
        )
        r = client.get(f"/api/livestream/sessions/{self.session.id}/status/")
        self.assertEqual(r.status_code, 403)

    def test_create_session_requires_teacher_context(self):
        from courses.models import Batch

        batch = Batch.objects.create(course=self.course, name="Batch A", code="A1")
        now = timezone.now()
        payload = {
            "title": "Ch2",
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "subject_id": str(self.subject.id),
            "batch_id": str(batch.id),
        }

        # Same teacher, but browsing in LEARNER context — must be rejected
        # before the serializer even runs (mirrors cancel/end/pause/detail).
        learner_client = self._client("learner", profile=self.self_profile)
        r = learner_client.post("/api/livestream/sessions/", payload, format="json")
        self.assertEqual(r.status_code, 403, r.content)

        teacher_client = self._client("teacher")
        r = teacher_client.post("/api/livestream/sessions/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)


class CourseSessionConsumerAuthTests(TransactionTestCase):
    """CourseSessionConsumer must accept a teacher assigned to the course's
    subject, not just an actively-enrolled student — regression for the
    teacher LiveSessions page's real-time updates, which the consumer
    previously rejected outright (enrollment-only check)."""

    def setUp(self):
        from courses.models import SubjectTeacher
        from enrollments.models import Enrollment

        self.teacher = User.objects.create_user(
            username="ct@x.com", email="ct@x.com", password="x"
        )
        self.student = User.objects.create_user(
            username="cs@x.com", email="cs@x.com", password="x"
        )
        self.outsider = User.objects.create_user(
            username="co@x.com", email="co@x.com", password="x"
        )
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        SubjectTeacher.objects.create(subject=self.subject, teacher=self.teacher)
        Enrollment.objects.create(
            user=self.student, course=self.course, status=Enrollment.STATUS_ACTIVE
        )

    def _connect(self, user):
        async def run():
            communicator = WebsocketCommunicator(
                CourseSessionConsumer.as_asgi(),
                f"/ws/course-sessions/{self.course.id}/",
            )
            # Bypassing JWTAuthMiddleware/URLRouter (out of scope here — this
            # test is about the consumer's own authorization gate).
            communicator.scope["url_route"] = {
                "kwargs": {"course_id": str(self.course.id)}
            }
            communicator.scope["user"] = user
            connected, _ = await communicator.connect()
            if connected:
                await communicator.disconnect()
            return connected

        return async_to_sync(run)()

    def test_teacher_assigned_to_course_is_accepted(self):
        self.assertTrue(self._connect(self.teacher))

    def test_enrolled_student_is_accepted(self):
        self.assertTrue(self._connect(self.student))

    def test_unrelated_user_is_rejected(self):
        self.assertFalse(self._connect(self.outsider))


class BroadcastCourseSessionsPayloadTests(TestCase):
    """broadcast_course_sessions_update must send the full
    LiveSessionListSerializer shape (computed_status/subject_name/
    course_name/description/can_join), not the old hand-rolled thin dict —
    both apps' cards render from these fields."""

    def setUp(self):
        self.teacher = User.objects.create_user(
            username="bt@x.com", email="bt@x.com", password="x"
        )
        board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Ch1",
            start_time=now + timedelta(hours=1), end_time=now + timedelta(hours=2),
            room_name="room_bcast", created_by=self.teacher,
            status=LiveSession.STATUS_SCHEDULED,
        )

    def test_broadcast_payload_has_full_serializer_shape(self):
        captured = {}

        class FakeChannelLayer:
            async def group_send(self, group, message):
                captured["group"] = group
                captured["message"] = message

        import livestream.views as views_module
        original = views_module.get_channel_layer
        views_module.get_channel_layer = lambda: FakeChannelLayer()
        try:
            views_module.broadcast_course_sessions_update(self.session)
        finally:
            views_module.get_channel_layer = original

        self.assertEqual(captured["group"], f"course_sessions_{self.course.id}")
        data = captured["message"]["data"]
        for key in (
            "id", "title", "description", "start_time", "end_time",
            "computed_status", "can_join", "subject_id", "subject_name",
            "course_name", "teacher_left_at", "status",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["computed_status"], "SCHEDULED")
        self.assertIsInstance(data, dict)
