"""Publishing, purging and the sweep — phase 4 of automatic class recording.

This is the phase that makes the feature usable, and the one with the most
ways to fail quietly:

  · nothing else polls Bunny for an automatic recording (a teacher's upload
    has a browser doing it; this has nobody),
  · the sweep is the ONLY backstop for a lost egress_ended webhook, without
    which the raw mp4 stays publicly readable forever,
  · a failed purge must leave raw_deleted_at NULL so the next sweep retries,
    rather than being recorded as done.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from courses.models import Batch, Board, Course, Subject
from courses.models_recordings import SessionRecording
from courses import services_recordings
from livestream.models import LiveSession, LiveSessionEgress
from livestream.services import bunny_stream
from livestream.services import egress as egress_svc

User = get_user_model()

SWEEP_ON = dict(
    BUNNY_LIBRARY_ID="12345",
    BUNNY_API_KEY="stream-key",
    BUNNY_CDN_HOST="video.b-cdn.net",
    BUNNY_EGRESS_ZONE="shiksha-class-egress",
    BUNNY_EGRESS_API_KEY="zone-password",
    BUNNY_EGRESS_REGION="sg",
    BUNNY_EGRESS_STORAGE_HOST="sg.storage.bunnycdn.com",
    BUNNY_EGRESS_PULL_HOST="shiksha-class-egress-pull.b-cdn.net",
)


def _bunny_video(status=4, length=3600, thumb="thumbnail.jpg"):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"status": status, "length": length,
                      "thumbnailFileName": thumb},
        text="",
    )


class SweepBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="sw_t@x.com", email="sw_t@x.com", password="x")
        board = Board.objects.create(name="SWBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="SW10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        self.batch = Batch.objects.create(course=self.course, name="SW-A")
        self.start = timezone.now() - timedelta(hours=3)
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Optics", start_time=self.start,
            end_time=self.start + timedelta(hours=1), room_name="room_sw",
            created_by=self.teacher,
        )

    def transcoding(self, status=2, **kwargs):
        """An egress whose recording exists but is still processing."""
        rec = SessionRecording.objects.create(
            subject=self.subject, batch=self.batch, live_session=self.session,
            title="Optics", bunny_video_id="guid-sw", uploaded_by=None,
            status=status, is_published=False,
        )
        row = LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_sw",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/sw/rand.mp4", recording=rec, **kwargs,
        )
        return row, rec


@override_settings(**SWEEP_ON)
class FinishRecordingTest(SweepBase):

    def test_publishes_and_purges_when_bunny_reports_finished(self):
        row, rec = self.transcoding(status=4)
        rec.duration_seconds = 3600
        rec.save(update_fields=["duration_seconds"])
        with patch.object(bunny_stream, "delete_raw_object") as purge, \
             patch.object(egress_svc, "_notify_recording_available"):
            self.assertTrue(egress_svc.finish_recording(row))
        rec.refresh_from_db()
        row.refresh_from_db()
        self.assertTrue(rec.is_published)
        purge.assert_called_once_with("class-egress/sw/rand.mp4")
        self.assertIsNotNone(row.raw_deleted_at)

    def test_does_nothing_while_still_transcoding(self):
        row, rec = self.transcoding(status=3)
        with patch.object(bunny_stream, "delete_raw_object") as purge:
            self.assertFalse(egress_svc.finish_recording(row))
        rec.refresh_from_db()
        self.assertFalse(rec.is_published)
        self.assertFalse(purge.called)
        self.assertIsNone(row.raw_deleted_at)

    def test_students_are_notified_only_once(self):
        row, rec = self.transcoding(status=4)
        with patch.object(bunny_stream, "delete_raw_object"), \
             patch.object(egress_svc, "_notify_recording_available") as notify:
            egress_svc.finish_recording(row)
            egress_svc.finish_recording(row)
        self.assertEqual(notify.call_count, 1)

    def test_failed_purge_leaves_it_retryable(self):
        """raw_deleted_at must stay NULL — an mp4 recorded as deleted while
        still public is the one outcome this design cannot tolerate."""
        row, rec = self.transcoding(status=4)
        with patch.object(bunny_stream, "delete_raw_object",
                          side_effect=RuntimeError("503 from Bunny")), \
             patch.object(egress_svc, "_notify_recording_available"):
            egress_svc.finish_recording(row)
        row.refresh_from_db()
        rec.refresh_from_db()
        self.assertIsNone(row.raw_deleted_at)
        self.assertIn("503", row.error)
        # Still watchable: publishing and purging are independent.
        self.assertTrue(rec.is_published)

    def test_a_later_sweep_retries_the_failed_purge(self):
        row, rec = self.transcoding(status=4)
        with patch.object(bunny_stream, "delete_raw_object",
                          side_effect=RuntimeError("503")), \
             patch.object(egress_svc, "_notify_recording_available"):
            egress_svc.finish_recording(row)
        with patch.object(bunny_stream, "delete_raw_object") as purge, \
             patch.object(egress_svc, "_notify_recording_available"):
            egress_svc.finish_recording(row)
        self.assertTrue(purge.called)
        row.refresh_from_db()
        self.assertIsNotNone(row.raw_deleted_at)

    def test_notification_failure_does_not_block_publish_or_purge(self):
        row, rec = self.transcoding(status=4)
        with patch("activity.signals._bulk_notify_students",
                   side_effect=RuntimeError("bell broken")), \
             patch.object(bunny_stream, "delete_raw_object") as purge:
            egress_svc.finish_recording(row)
        rec.refresh_from_db()
        row.refresh_from_db()
        self.assertTrue(rec.is_published)
        self.assertTrue(purge.called)
        self.assertIsNotNone(row.raw_deleted_at)


@override_settings(**SWEEP_ON)
class SweepTest(SweepBase):

    def test_polls_bunny_and_finishes_a_ready_recording(self):
        row, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=4)), \
             patch.object(bunny_stream, "delete_raw_object"), \
             patch.object(egress_svc, "_notify_recording_available"):
            counts = egress_svc.sweep_recordings()
        rec.refresh_from_db()
        self.assertEqual(rec.status, 4)
        self.assertEqual(rec.duration_seconds, 3600)
        self.assertTrue(rec.is_published)
        self.assertEqual(counts["finished"], 1)

    def test_thumbnail_is_captured_on_completion(self):
        row, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=4, thumb="t.jpg")), \
             patch.object(bunny_stream, "delete_raw_object"), \
             patch.object(egress_svc, "_notify_recording_available"):
            egress_svc.sweep_recordings()
        rec.refresh_from_db()
        self.assertEqual(
            rec.thumbnail_url, "https://video.b-cdn.net/guid-sw/t.jpg")

    def test_a_still_transcoding_recording_is_left_alone(self):
        row, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=3, length=0)), \
             patch.object(bunny_stream, "delete_raw_object") as purge:
            egress_svc.sweep_recordings()
        rec.refresh_from_db()
        self.assertEqual(rec.status, 3)
        self.assertFalse(rec.is_published)
        self.assertFalse(purge.called)

    def test_a_bunny_error_status_is_not_published(self):
        row, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=5, length=0)), \
             patch.object(bunny_stream, "delete_raw_object") as purge:
            egress_svc.sweep_recordings()
        rec.refresh_from_db()
        self.assertEqual(rec.status, 5)
        self.assertFalse(rec.is_published)
        self.assertFalse(purge.called)

    def test_lost_webhook_backstop_drains_the_fetch_queue(self):
        """The gap phase 3 left open: nothing but this notices an egress that
        completed while its webhook went missing."""
        row = LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_lost",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/lost/x.mp4",
        )
        self.assertTrue(row.awaiting_stream_fetch)
        with patch.object(bunny_stream, "create_video_slot",
                          return_value="guid-lost") as slot, \
             patch.object(bunny_stream, "fetch_into_video"), \
             patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=0, length=0)):
            counts = egress_svc.sweep_recordings()
        self.assertTrue(slot.called)
        self.assertEqual(counts["fetched"], 1)
        row.refresh_from_db()
        self.assertIsNotNone(row.recording_id)

    def test_one_bad_recording_does_not_abort_the_sweep(self):
        bad, bad_rec = self.transcoding(status=2)
        other_session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Waves", start_time=self.start,
            end_time=self.start + timedelta(hours=1), room_name="room_sw2",
            created_by=self.teacher,
        )
        good_rec = SessionRecording.objects.create(
            subject=self.subject, batch=self.batch,
            live_session=other_session, title="Waves",
            bunny_video_id="guid-good", uploaded_by=None, status=4,
            is_published=False,
        )
        LiveSessionEgress.objects.create(
            session=other_session, egress_id="EG_good",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/good/x.mp4", recording=good_rec,
        )
        with patch.object(egress_svc, "finish_recording",
                          side_effect=[RuntimeError("boom"), True]), \
             patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=4)):
            egress_svc.sweep_recordings()  # must not raise

    def test_exhausted_fetch_attempts_are_not_retried(self):
        LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_dead",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/dead/x.mp4",
            fetch_attempts=egress_svc.MAX_FETCH_ATTEMPTS,
        )
        with patch.object(bunny_stream, "create_video_slot") as slot:
            egress_svc.sweep_recordings()
        self.assertFalse(slot.called)

    def test_old_attempts_fall_out_of_the_window(self):
        """An unbounded scan would grow with every class ever held."""
        row = LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_ancient",
            status=LiveSessionEgress.STATUS_COMPLETE,
            storage_key="class-egress/old/x.mp4",
        )
        LiveSessionEgress.objects.filter(pk=row.pk).update(
            requested_at=timezone.now() - timedelta(
                days=egress_svc.SWEEP_WINDOW_DAYS + 1))
        with patch.object(bunny_stream, "create_video_slot") as slot:
            counts = egress_svc.sweep_recordings()
        self.assertFalse(slot.called)
        self.assertEqual(counts["fetched"], 0)

    def test_sweep_is_a_no_op_with_nothing_in_flight(self):
        with patch("courses.services_recordings.requests.get") as get:
            counts = egress_svc.sweep_recordings()
        self.assertFalse(get.called)
        self.assertEqual(counts, {"fetched": 0, "polled": 0, "finished": 0})


@override_settings(**SWEEP_ON)
class PurgeCallTest(SweepBase):
    """The delete goes to the NATIVE storage host, not the S3 one."""

    def test_delete_targets_the_native_edge_storage_api(self):
        with patch("livestream.services.bunny_stream.requests.delete") as dele:
            dele.return_value = SimpleNamespace(status_code=200, text="")
            bunny_stream.delete_raw_object("class-egress/a/b.mp4")
        url = dele.call_args[0][0]
        self.assertEqual(
            url,
            "https://sg.storage.bunnycdn.com/shiksha-class-egress/"
            "class-egress/a/b.mp4",
        )
        self.assertEqual(
            dele.call_args[1]["headers"]["AccessKey"], "zone-password")

    def test_already_gone_counts_as_deleted(self):
        """404 means the object is gone, which is the goal."""
        with patch("livestream.services.bunny_stream.requests.delete") as dele:
            dele.return_value = SimpleNamespace(status_code=404, text="")
            bunny_stream.delete_raw_object("a/b.mp4")  # must not raise

    def test_other_errors_raise_so_the_purge_is_retried(self):
        with patch("livestream.services.bunny_stream.requests.delete") as dele:
            dele.return_value = SimpleNamespace(status_code=403, text="denied")
            with self.assertRaises(RuntimeError):
                bunny_stream.delete_raw_object("a/b.mp4")

    @override_settings(BUNNY_EGRESS_API_KEY="")
    def test_missing_credentials_raise_rather_than_silently_skipping(self):
        with self.assertRaises(RuntimeError):
            bunny_stream.delete_raw_object("a/b.mp4")


@override_settings(**SWEEP_ON)
class SharedStatusRefreshTest(SweepBase):
    """CheckVideoStatusView and the sweep now share one implementation. The
    duration-capture rule below is the bug fix that made extracting it
    worthwhile rather than copying."""

    def test_settled_recording_is_not_re_fetched(self):
        _, rec = self.transcoding(status=4)
        rec.duration_seconds = 100
        rec.save(update_fields=["duration_seconds"])
        with patch("courses.services_recordings.requests.get") as get:
            self.assertFalse(services_recordings.refresh_from_bunny(rec))
        self.assertFalse(get.called)

    def test_finished_recording_with_no_duration_is_still_fetched(self):
        """The load-bearing half: on status alone, a recording that reached
        READY before duration capture existed could never acquire one."""
        _, rec = self.transcoding(status=4)
        self.assertIsNone(rec.duration_seconds)
        with patch("courses.services_recordings.requests.get",
                   return_value=_bunny_video(status=4, length=42)) as get:
            self.assertTrue(services_recordings.refresh_from_bunny(rec))
        self.assertTrue(get.called)
        rec.refresh_from_db()
        self.assertEqual(rec.duration_seconds, 42)

    def test_bunny_outage_does_not_raise(self):
        _, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   side_effect=RuntimeError("connection reset")):
            self.assertFalse(services_recordings.refresh_from_bunny(rec))

    def test_malformed_bunny_body_does_not_raise(self):
        """The original inline version wrapped parsing and saving in the same
        try as the request. A client polling status must not get a 500 because
        Bunny returned something unparseable."""
        _, rec = self.transcoding(status=2)

        def _boom():
            raise ValueError("not json")

        with patch("courses.services_recordings.requests.get",
                   return_value=SimpleNamespace(
                       status_code=200, json=_boom, text="<html>")):
            self.assertFalse(services_recordings.refresh_from_bunny(rec))
        rec.refresh_from_db()
        self.assertEqual(rec.status, 2)

    def test_non_200_is_ignored(self):
        _, rec = self.transcoding(status=2)
        with patch("courses.services_recordings.requests.get",
                   return_value=SimpleNamespace(
                       status_code=500, json=lambda: {}, text="")):
            self.assertFalse(services_recordings.refresh_from_bunny(rec))
        rec.refresh_from_db()
        self.assertEqual(rec.status, 2)
