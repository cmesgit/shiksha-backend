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

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from courses.models import Board, Course, Subject
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
