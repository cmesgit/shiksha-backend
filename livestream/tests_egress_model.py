"""LiveSessionEgress schema guarantees — phase 0 of automatic class recording.

Two of these are not cosmetic. `awaiting_stream_fetch` is the definition of
the work queue the phase-3 Bunny Stream fetch task will drain, and the
unique-when-set constraint on `egress_id` is what stops a redelivered
LiveKit webhook from forking one egress into two rows — both are easier to
get subtly wrong later than to pin now.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from courses.models import Board, Course, Subject
from courses.models_recordings import SessionRecording
from livestream.models import LiveSession, LiveSessionEgress

User = get_user_model()


class EgressModelTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="eg_t@x.com", email="eg_t@x.com", password="x")
        board = Board.objects.create(name="EGBoard", board_type=Board.TYPE_CENTRAL)
        cls.course = Course.objects.create(board=board, title="EG10", class_level=10)
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        now = timezone.now()
        cls.session = LiveSession.objects.create(
            course=cls.course, subject=cls.subject, title="Egress class",
            start_time=now, end_time=now + timedelta(hours=1),
            room_name="room-egress-1", created_by=cls.teacher,
        )

    def _egress(self, **kwargs):
        return LiveSessionEgress.objects.create(session=self.session, **kwargs)

    def test_defaults_to_requested_with_no_egress_id(self):
        """The row is written BEFORE the LiveKit start call returns, so that
        a start that throws still leaves a trail of the attempt."""
        row = self._egress()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_REQUESTED)
        self.assertEqual(row.egress_id, "")
        self.assertFalse(row.is_terminal)
        self.assertFalse(row.awaiting_stream_fetch)

    def test_several_attempts_may_share_one_session(self):
        """A teacher who drops and rejoins produces a second attempt; the
        first must not be overwritten or rejected."""
        self._egress(egress_id="EG_aaa", status=LiveSessionEgress.STATUS_FAILED)
        self._egress(egress_id="EG_bbb", status=LiveSessionEgress.STATUS_ACTIVE)
        self.assertEqual(self.session.egresses.count(), 2)

    def test_egress_id_is_unique_once_set(self):
        """A redelivered webhook must collapse onto the existing row rather
        than forking the attempt in two."""
        self._egress(egress_id="EG_dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._egress(egress_id="EG_dup")

    def test_blank_egress_ids_do_not_collide(self):
        """The constraint is deliberately partial: several attempts can be
        mid-flight with no id yet, and a plain unique index would reject the
        second one."""
        self._egress()
        self._egress()
        self.assertEqual(
            self.session.egresses.filter(egress_id="").count(), 2
        )

    def test_awaiting_stream_fetch_only_for_completed_unfetched_files(self):
        complete = self._egress(
            egress_id="EG_done", status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/x/abc.mp4",
        )
        self.assertTrue(complete.awaiting_stream_fetch)

        still_running = self._egress(
            egress_id="EG_live", status=LiveSessionEgress.STATUS_ACTIVE,
            storage_key="class-egress/x/def.mp4",
        )
        self.assertFalse(still_running.awaiting_stream_fetch)

        no_file = self._egress(
            egress_id="EG_nofile", status=LiveSessionEgress.STATUS_COMPLETE,
        )
        self.assertFalse(no_file.awaiting_stream_fetch)

    def test_already_fetched_attempt_leaves_the_queue(self):
        recording = SessionRecording.objects.create(
            subject=self.subject, title="Egress class", bunny_video_id="vid-eg",
            uploaded_by=None, live_session=self.session,
        )
        row = self._egress(
            egress_id="EG_fetched", status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/x/ghi.mp4", recording=recording,
        )
        self.assertFalse(row.awaiting_stream_fetch)

    def test_deleting_the_recording_keeps_the_egress_audit_row(self):
        """SET_NULL: the trail of how a recording was produced must outlive
        the recording itself, since that is exactly what you go looking for
        when one disappears."""
        recording = SessionRecording.objects.create(
            subject=self.subject, title="Egress class", bunny_video_id="vid-eg2",
            uploaded_by=None, live_session=self.session,
        )
        row = self._egress(egress_id="EG_orphan", recording=recording)
        recording.delete()
        row.refresh_from_db()
        self.assertIsNone(row.recording_id)

    def test_deleting_the_session_removes_its_egress_rows(self):
        """CASCADE here, unlike `recording`: an egress attempt has no meaning
        without the class it was recording."""
        self._egress(egress_id="EG_cascade")
        self.session.delete()
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_terminal_statuses_cover_every_end_state(self):
        for status in (
            LiveSessionEgress.STATUS_START_FAILED,
            LiveSessionEgress.STATUS_COMPLETE,
            LiveSessionEgress.STATUS_FAILED,
            LiveSessionEgress.STATUS_ABORTED,
            LiveSessionEgress.STATUS_LIMIT_REACHED,
        ):
            with self.subTest(status=status):
                self.assertTrue(self._egress(status=status).is_terminal)

        for status in (
            LiveSessionEgress.STATUS_REQUESTED,
            LiveSessionEgress.STATUS_STARTING,
            LiveSessionEgress.STATUS_ACTIVE,
            LiveSessionEgress.STATUS_ENDING,
        ):
            with self.subTest(status=status):
                self.assertFalse(self._egress(status=status).is_terminal)
