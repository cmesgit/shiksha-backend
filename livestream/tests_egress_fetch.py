"""The Bunny Stream handoff — phase 3 of automatic class recording.

The mp4 exists in Bunny Storage; this is where it becomes a SessionRecording
the existing playback path can serve. Every Bunny HTTP call is patched, so
what these tests exercise is the part that can actually corrupt data: the
idempotence claim, the ordering that decides what a mid-handoff crash leaves
behind, and the fields inherited from the live class.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from courses.models import Batch, Board, Course, Subject
from courses.models_recordings import SessionRecording
from livestream.models import LiveSession, LiveSessionEgress
from livestream.services import bunny_stream
from livestream.services import egress as egress_svc

User = get_user_model()

STREAM_ON = dict(
    BUNNY_LIBRARY_ID="12345",
    BUNNY_API_KEY="stream-key",
    BUNNY_STREAM_URL="https://video.bunnycdn.com",
    BUNNY_EGRESS_PULL_HOST="shiksha-class-egress-pull.b-cdn.net",
)


class FetchBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="fx_t@x.com", email="fx_t@x.com", password="x")
        board = Board.objects.create(name="FXBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="FX10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Bio")
        self.batch = Batch.objects.create(course=self.course, name="FX-A")
        self.start = timezone.now() - timedelta(hours=2)
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Photosynthesis", start_time=self.start,
            end_time=self.start + timedelta(hours=1), room_name="room_fx",
            created_by=self.teacher,
        )

    def ready_egress(self, **kwargs):
        kwargs.setdefault("status", LiveSessionEgress.STATUS_COMPLETE)
        kwargs.setdefault("storage_key", "class-egress/abc/def123.mp4")
        kwargs.setdefault("egress_id", "EG_done")
        return LiveSessionEgress.objects.create(session=self.session, **kwargs)


@override_settings(**STREAM_ON)
class HandoffTest(FetchBase):

    def test_creates_a_recording_inheriting_the_classes_scope(self):
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot",
                          return_value="guid-1") as slot, \
             patch.object(bunny_stream, "fetch_into_video") as fetch:
            rec = egress_svc.hand_off_to_stream(row.pk)

        self.assertTrue(slot.called)
        self.assertTrue(fetch.called)
        self.assertEqual(rec.subject_id, self.subject.id)
        self.assertEqual(rec.batch_id, self.batch.id)
        self.assertEqual(rec.live_session_id, self.session.id)
        self.assertEqual(rec.title, "Photosynthesis")
        self.assertEqual(rec.session_date, self.start.date())
        self.assertEqual(rec.bunny_video_id, "guid-1")

    def test_recording_has_no_uploader(self):
        """The reason uploaded_by was made nullable in phase 0."""
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot", return_value="g"), \
             patch.object(bunny_stream, "fetch_into_video"):
            rec = egress_svc.hand_off_to_stream(row.pk)
        self.assertIsNone(rec.uploaded_by_id)

    def test_recording_starts_unpublished(self):
        """Students filter on is_published; a video that cannot play yet must
        not be offered to them. Teachers see it as Pending."""
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot", return_value="g"), \
             patch.object(bunny_stream, "fetch_into_video"):
            rec = egress_svc.hand_off_to_stream(row.pk)
        self.assertFalse(rec.is_published)
        self.assertEqual(rec.status, 0)

    def test_egress_row_is_linked_to_its_recording(self):
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot", return_value="g"), \
             patch.object(bunny_stream, "fetch_into_video"):
            rec = egress_svc.hand_off_to_stream(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.recording_id, rec.pk)
        self.assertFalse(row.awaiting_stream_fetch)

    def test_fetch_is_given_the_pull_zone_url_for_the_object(self):
        row = self.ready_egress(storage_key="class-egress/s/rand.mp4")
        with patch.object(bunny_stream, "create_video_slot", return_value="g"), \
             patch.object(bunny_stream, "fetch_into_video") as fetch:
            egress_svc.hand_off_to_stream(row.pk)
        _, url = fetch.call_args[0]
        self.assertEqual(
            url,
            "https://shiksha-class-egress-pull.b-cdn.net/class-egress/s/rand.mp4",
        )


@override_settings(**STREAM_ON)
class HandoffIdempotenceTest(FetchBase):
    """Both the webhook and (from phase 4) a sweep can reach one row. Two
    handoffs mean two Bunny Stream videos for one class, one of which bills
    forever with nothing pointing at it."""

    def test_second_handoff_is_a_no_op(self):
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot",
                          return_value="g") as slot, \
             patch.object(bunny_stream, "fetch_into_video"):
            first = egress_svc.hand_off_to_stream(row.pk)
            second = egress_svc.hand_off_to_stream(row.pk)
        self.assertEqual(slot.call_count, 1)
        self.assertIsNone(second)
        self.assertEqual(SessionRecording.objects.count(), 1)
        self.assertEqual(first.pk, SessionRecording.objects.get().pk)

    def test_an_unfinished_egress_is_not_handed_off(self):
        row = self.ready_egress(status=LiveSessionEgress.STATUS_ACTIVE)
        with patch.object(bunny_stream, "create_video_slot") as slot:
            self.assertIsNone(egress_svc.hand_off_to_stream(row.pk))
        self.assertFalse(slot.called)

    def test_an_egress_with_no_file_is_not_handed_off(self):
        row = self.ready_egress(storage_key="")
        with patch.object(bunny_stream, "create_video_slot") as slot:
            self.assertIsNone(egress_svc.hand_off_to_stream(row.pk))
        self.assertFalse(slot.called)

    def test_attempts_are_capped(self):
        row = self.ready_egress(
            fetch_attempts=egress_svc.MAX_FETCH_ATTEMPTS)
        with patch.object(bunny_stream, "create_video_slot") as slot:
            self.assertIsNone(egress_svc.hand_off_to_stream(row.pk))
        self.assertFalse(slot.called)

    def test_each_handoff_counts_an_attempt(self):
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot",
                          side_effect=RuntimeError("bunny down")):
            egress_svc.hand_off_to_stream(row.pk)
            egress_svc.hand_off_to_stream(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.fetch_attempts, 2)

    def test_missing_egress_row_is_handled(self):
        self.assertIsNone(egress_svc.hand_off_to_stream(999999))


@override_settings(**STREAM_ON)
class HandoffFailureTest(FetchBase):

    def test_slot_failure_creates_no_recording(self):
        """Nothing partial: no Bunny video exists, so no row should either."""
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot",
                          side_effect=RuntimeError("401 unauthorized")):
            self.assertIsNone(egress_svc.hand_off_to_stream(row.pk))
        row.refresh_from_db()
        self.assertEqual(SessionRecording.objects.count(), 0)
        self.assertIn("401", row.error)
        self.assertIsNone(row.recording_id)

    def test_fetch_failure_keeps_the_visible_recording_row(self):
        """The ordering decision: a Bunny video now exists, so the row stays
        so it is visible and retryable, not an invisible billed orphan."""
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot", return_value="g"), \
             patch.object(bunny_stream, "fetch_into_video",
                          side_effect=RuntimeError("fetch 404")):
            rec = egress_svc.hand_off_to_stream(row.pk)
        row.refresh_from_db()
        self.assertIsNotNone(rec)
        self.assertEqual(row.recording_id, rec.pk)
        self.assertIn("404", row.error)
        self.assertFalse(rec.is_published)

    @override_settings(BUNNY_EGRESS_PULL_HOST="")
    def test_missing_pull_zone_fails_fast_without_retrying(self):
        """No amount of retrying invents a pull zone, so this burns the
        attempts budget immediately and records why."""
        row = self.ready_egress()
        with patch.object(bunny_stream, "create_video_slot") as slot:
            self.assertIsNone(egress_svc.hand_off_to_stream(row.pk))
        self.assertFalse(slot.called)
        row.refresh_from_db()
        self.assertIn("BUNNY_EGRESS_PULL_HOST", row.error)
        self.assertGreaterEqual(
            row.fetch_attempts, egress_svc.MAX_FETCH_ATTEMPTS)


@override_settings(**STREAM_ON)
class PublicUrlTest(FetchBase):

    def test_url_is_built_on_the_egress_pull_zone(self):
        self.assertEqual(
            bunny_stream.public_url_for("class-egress/a/b.mp4"),
            "https://shiksha-class-egress-pull.b-cdn.net/class-egress/a/b.mp4",
        )

    def test_leading_slash_does_not_double_up(self):
        self.assertEqual(
            bunny_stream.public_url_for("/class-egress/a/b.mp4"),
            "https://shiksha-class-egress-pull.b-cdn.net/class-egress/a/b.mp4",
        )

    @override_settings(BUNNY_EGRESS_PULL_HOST="")
    def test_no_pull_zone_yields_no_url(self):
        self.assertEqual(bunny_stream.public_url_for("a/b.mp4"), "")

    @override_settings(BUNNY_EGRESS_PULL_HOST="egress.b-cdn.net",
                       BUNNY_STORAGE_CDN_HOST="cms.b-cdn.net",
                       BUNNY_CDN_HOST="video.b-cdn.net")
    def test_egress_pull_zone_is_not_the_cms_or_stream_host(self):
        """Three different Bunny hosts; crossing them would serve class
        recordings from a zone that does not contain them."""
        url = bunny_stream.public_url_for("k.mp4")
        self.assertIn("egress.b-cdn.net", url)
        self.assertNotIn("cms.b-cdn.net", url)
        self.assertNotIn("video.b-cdn.net", url)


@override_settings(**STREAM_ON)
class StreamApiCallTest(FetchBase):
    """The two HTTP calls, at the requests boundary."""

    def test_slot_creation_posts_to_the_library(self):
        with patch("livestream.services.bunny_stream.requests.post") as post:
            post.return_value = SimpleNamespace(
                status_code=200, json=lambda: {"guid": "g-1"}, text="")
            guid = bunny_stream.create_video_slot("Photosynthesis")
        self.assertEqual(guid, "g-1")
        url = post.call_args[0][0]
        self.assertEqual(url, "https://video.bunnycdn.com/library/12345/videos")
        self.assertEqual(
            post.call_args[1]["headers"]["AccessKey"], "stream-key")
        self.assertEqual(
            post.call_args[1]["json"], {"title": "Photosynthesis"})

    def test_slot_creation_raises_without_a_guid(self):
        with patch("livestream.services.bunny_stream.requests.post") as post:
            post.return_value = SimpleNamespace(
                status_code=200, json=lambda: {}, text="{}")
            with self.assertRaises(RuntimeError):
                bunny_stream.create_video_slot("x")

    def test_slot_creation_raises_on_error_status(self):
        with patch("livestream.services.bunny_stream.requests.post") as post:
            post.return_value = SimpleNamespace(
                status_code=401, json=lambda: {}, text="unauthorized")
            with self.assertRaises(RuntimeError):
                bunny_stream.create_video_slot("x")

    def test_fetch_posts_the_url_to_the_videos_fetch_endpoint(self):
        with patch("livestream.services.bunny_stream.requests.post") as post:
            post.return_value = SimpleNamespace(
                status_code=200, json=lambda: {}, text="")
            bunny_stream.fetch_into_video("g-1", "https://pull/x.mp4")
        self.assertEqual(
            post.call_args[0][0],
            "https://video.bunnycdn.com/library/12345/videos/g-1/fetch",
        )
        self.assertEqual(
            post.call_args[1]["json"], {"url": "https://pull/x.mp4"})

    def test_fetch_raises_on_error_status(self):
        with patch("livestream.services.bunny_stream.requests.post") as post:
            post.return_value = SimpleNamespace(
                status_code=500, json=lambda: {}, text="boom")
            with self.assertRaises(RuntimeError):
                bunny_stream.fetch_into_video("g", "https://pull/x.mp4")


@override_settings(**STREAM_ON)
class EndToEndFromWebhookTest(FetchBase):
    """egress_ended → recording, through the real event handler. CELERY_TASK_
    ALWAYS_EAGER in settings_test runs the queued task inline."""

    def _ended_event(self, egress_id="EG_done"):
        info = SimpleNamespace(
            egress_id=egress_id, room_name="room_fx", status=3,
            started_at=0, ended_at=0, error="",
            file_results=[SimpleNamespace(
                filename="class-egress/abc/def123.mp4",
                location="", size=1024, duration=3_600_000_000_000,
                started_at=0, ended_at=0)],
            file=None,
        )
        return SimpleNamespace(
            event="egress_ended", id="evt-ended", egress_info=info,
            room=None, participant=None, created_at=0)

    def test_egress_ended_produces_a_playable_recording_row(self):
        row = LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_done",
            status=LiveSessionEgress.STATUS_ACTIVE, storage_key="pending",
        )
        with patch.object(bunny_stream, "create_video_slot",
                          return_value="guid-e2e"), \
             patch.object(bunny_stream, "fetch_into_video") as fetch:
            with self.captureOnCommitCallbacks(execute=True):
                egress_svc.apply_egress_event(
                    self._ended_event(), room_name="room_fx")

        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_COMPLETE)
        self.assertEqual(row.duration_seconds, 3600)
        self.assertIsNotNone(row.recording_id)
        rec = row.recording
        self.assertEqual(rec.bunny_video_id, "guid-e2e")
        self.assertEqual(rec.live_session_id, self.session.id)
        self.assertFalse(rec.is_published)
        self.assertTrue(fetch.called)

    def test_a_mid_egress_update_does_not_trigger_a_handoff(self):
        LiveSessionEgress.objects.create(
            session=self.session, egress_id="EG_done",
            status=LiveSessionEgress.STATUS_STARTING, storage_key="k.mp4",
        )
        evt = self._ended_event()
        evt.egress_info.status = 1  # EGRESS_ACTIVE
        evt.egress_info.file_results = []
        with patch.object(bunny_stream, "create_video_slot") as slot:
            with self.captureOnCommitCallbacks(execute=True):
                egress_svc.apply_egress_event(evt, room_name="room_fx")
        self.assertFalse(slot.called)
        self.assertEqual(SessionRecording.objects.count(), 0)
