"""Egress webhook events — phase 2 of automatic class recording.

The three events (egress_started / egress_updated / egress_ended) all carry
one EgressInfo payload, so one handler folds any of them into the attempt's
row. What these tests mostly exist to pin are the four ways that goes wrong
quietly:

  · egress events have NO event.room, so the room name has to come off
    egress_info or every event logs unattached to its class,
  · LiveKit reports timestamps and durations in NANOSECONDS,
  · webhook delivery is neither ordered nor exactly-once, so a late
    egress_updated must not resurrect a finished attempt,
  · a start call whose response is lost still leaves a real, billed egress
    running that only these events can reconcile.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from courses.models import Board, Course, Subject
from livestream import views as lv
from livestream.models import LiveSession, LiveSessionEgress
from livestream.services import egress as egress_svc

User = get_user_model()

NS = 1_000_000_000  # nanoseconds per second, as LiveKit reports them

# EgressStatus ints, which is what the wire actually carries.
STARTING, ACTIVE, ENDING, COMPLETE, FAILED = 0, 1, 2, 3, 4


def _file(filename="class-egress/x/abc.mp4", size=524_288_000, duration_s=3600):
    return SimpleNamespace(
        filename=filename,
        location=f"https://sg-s3.storage.bunnycdn.com/zone/{filename}",
        size=size,
        duration=duration_s * NS,
        started_at=0,
        ended_at=0,
    )


def _event(egress_id="EG_1", status=ACTIVE, room="room_ev", *,
           started_s=None, ended_s=None, error="", files=None):
    """An egress webhook event. Deliberately has NO `room` attribute — that
    is the whole point of the trap this pins."""
    info = SimpleNamespace(
        egress_id=egress_id,
        room_name=room,
        status=status,
        started_at=(started_s * NS) if started_s else 0,
        ended_at=(ended_s * NS) if ended_s else 0,
        error=error,
        file_results=files if files is not None else [],
        file=None,
    )
    return SimpleNamespace(
        event="egress_updated", id=f"evt-{egress_id}-{status}",
        egress_info=info, room=None, participant=None, created_at=0,
    )


class EgressEventBase(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="ev_t@x.com", email="ev_t@x.com", password="x")
        board = Board.objects.create(name="EVBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="EV10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Chem")
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Chem",
            start_time=now, end_time=now + timedelta(hours=1),
            room_name="room_ev", created_by=self.teacher,
        )

    def _row(self, **kwargs):
        kwargs.setdefault("storage_key", "class-egress/x/abc.mp4")
        return LiveSessionEgress.objects.create(session=self.session, **kwargs)


class RoomNameResolutionTest(EgressEventBase):
    """The trap: `_event_room_name` read only event.room.name, and egress
    events do not have one."""

    def test_room_name_comes_from_egress_info(self):
        self.assertEqual(lv._event_room_name(_event(room="room_ev")), "room_ev")

    def test_participant_events_still_use_event_room(self):
        evt = SimpleNamespace(room=SimpleNamespace(name="room_other"),
                              egress_info=None)
        self.assertEqual(lv._event_room_name(evt), "room_other")

    def test_event_room_wins_when_both_are_present(self):
        evt = SimpleNamespace(
            room=SimpleNamespace(name="room_real"),
            egress_info=SimpleNamespace(room_name="room_stale"),
        )
        self.assertEqual(lv._event_room_name(evt), "room_real")

    def test_dedupe_key_distinguishes_two_egresses_in_one_room(self):
        """Egress events have no participant identity, so without the egress
        id in the composite key two concurrent attempts in one room at the
        same second collapse into one row and the second is dropped."""
        a = _event("EG_a", status=ACTIVE)
        b = _event("EG_b", status=ACTIVE)
        for e in (a, b):
            e.id = None  # force the composite fallback
        self.assertNotEqual(lv._event_dedupe_id(a), lv._event_dedupe_id(b))


class EgressEventApplyTest(EgressEventBase):

    def test_status_is_recorded_against_the_matching_row(self):
        row = self._row(egress_id="EG_1", status=LiveSessionEgress.STATUS_STARTING)
        egress_svc.apply_egress_event(_event("EG_1", ACTIVE), room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_ACTIVE)

    def test_nanosecond_timestamps_are_converted_to_real_times(self):
        """Read as seconds these land in 1970 and nothing complains."""
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, started_s=1_756_000_000,
                   ended_s=1_756_003_600, files=[_file()]),
            room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertEqual(row.started_at.year, 2025)
        self.assertEqual(row.ended_at.year, 2025)
        self.assertEqual(
            int((row.ended_at - row.started_at).total_seconds()), 3600,
        )

    def test_nanosecond_duration_is_converted_to_seconds(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, files=[_file(duration_s=3600)]),
            room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertEqual(row.duration_seconds, 3600)

    def test_file_size_is_recorded(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, files=[_file(size=12345)]),
            room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertEqual(row.file_size_bytes, 12345)

    def test_reported_filename_overrides_the_requested_key(self):
        """What LiveKit actually wrote is what phase 3 has to fetch."""
        row = self._row(egress_id="EG_1", storage_key="class-egress/x/asked.mp4")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE,
                   files=[_file(filename="class-egress/x/actual.mp4")]),
            room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertEqual(row.storage_key, "class-egress/x/actual.mp4")

    def test_deprecated_singular_file_field_is_also_read(self):
        """Which of `file` / `file_results` is populated has moved across
        LiveKit versions, and an empty result costs phase 3 its object key."""
        row = self._row(egress_id="EG_1")
        evt = _event("EG_1", COMPLETE)
        evt.egress_info.file_results = []
        evt.egress_info.file = _file(filename="class-egress/x/legacy.mp4")
        egress_svc.apply_egress_event(evt, room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.storage_key, "class-egress/x/legacy.mp4")

    def test_error_is_persisted_on_a_failed_egress(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", FAILED, error="upload failed: 403"),
            room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_FAILED)
        self.assertIn("403", row.error)
        self.assertTrue(row.is_terminal)

    def test_completed_egress_joins_the_fetch_queue(self):
        """The handoff to phase 3, expressed as the derived property rather
        than a second status column."""
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, files=[_file()]), room_name="room_ev",
        )
        row.refresh_from_db()
        self.assertTrue(row.awaiting_stream_fetch)

    def test_unknown_egress_id_is_ignored_not_created(self):
        egress_svc.apply_egress_event(
            _event("EG_ghost", ACTIVE, room="room_nowhere"),
            room_name="room_nowhere",
        )
        self.assertEqual(LiveSessionEgress.objects.count(), 0)

    def test_event_with_no_egress_info_is_ignored(self):
        evt = SimpleNamespace(egress_info=None, room=None)
        self.assertIsNone(egress_svc.apply_egress_event(evt))


class EgressEventOrderingTest(EgressEventBase):
    """LiveKit retries webhooks and promises no ordering. The webhook sink's
    per-event_id dedupe collapses redeliveries of ONE event but cannot
    reorder two different ones."""

    def test_late_update_does_not_resurrect_a_completed_egress(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, files=[_file()]), room_name="room_ev")
        egress_svc.apply_egress_event(
            _event("EG_1", ACTIVE), room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_COMPLETE)

    def test_late_update_does_not_resurrect_a_failed_egress(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", FAILED, error="boom"), room_name="room_ev")
        egress_svc.apply_egress_event(
            _event("EG_1", ENDING), room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.status, LiveSessionEgress.STATUS_FAILED)

    def test_first_start_time_is_kept_not_overwritten(self):
        row = self._row(egress_id="EG_1")
        egress_svc.apply_egress_event(
            _event("EG_1", ACTIVE, started_s=1_756_000_000), room_name="room_ev")
        row.refresh_from_db()
        first = row.started_at
        egress_svc.apply_egress_event(
            _event("EG_1", COMPLETE, started_s=1_756_009_999, files=[_file()]),
            room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.started_at, first)


class OrphanAdoptionTest(EgressEventBase):
    """A start call whose response never came back still left a real, billed
    egress running. Without adoption it records nothing, bills anyway, and
    its REQUESTED row blocks every retry for the session."""

    def test_event_adopts_a_row_that_never_got_an_id(self):
        row = self._row(status=LiveSessionEgress.STATUS_REQUESTED)
        self.assertEqual(row.egress_id, "")
        egress_svc.apply_egress_event(
            _event("EG_late", ACTIVE), room_name="room_ev")
        row.refresh_from_db()
        self.assertEqual(row.egress_id, "EG_late")
        self.assertEqual(row.status, LiveSessionEgress.STATUS_ACTIVE)
        self.assertEqual(LiveSessionEgress.objects.count(), 1)

    def test_adoption_does_not_steal_a_terminal_row(self):
        """A START_FAILED attempt is finished; a later unrelated egress must
        not be grafted onto it."""
        self._row(status=LiveSessionEgress.STATUS_START_FAILED, error="nope")
        egress_svc.apply_egress_event(
            _event("EG_other", ACTIVE), room_name="room_ev")
        self.assertEqual(
            LiveSessionEgress.objects.filter(egress_id="EG_other").count(), 0,
        )

    def test_adoption_does_not_cross_sessions(self):
        other = LiveSession.objects.create(
            course=self.course, subject=self.subject, title="Other",
            start_time=self.session.start_time,
            end_time=self.session.end_time, room_name="room_other",
            created_by=self.teacher,
        )
        LiveSessionEgress.objects.create(session=other, storage_key="k")
        egress_svc.apply_egress_event(
            _event("EG_x", ACTIVE, room="room_ev"), room_name="room_ev")
        self.assertEqual(
            LiveSessionEgress.objects.filter(egress_id="EG_x").count(), 0,
        )

    def test_a_matched_id_never_triggers_adoption(self):
        matched = self._row(egress_id="EG_1")
        orphan = self._row()
        egress_svc.apply_egress_event(
            _event("EG_1", ACTIVE), room_name="room_ev")
        matched.refresh_from_db()
        orphan.refresh_from_db()
        self.assertEqual(matched.status, LiveSessionEgress.STATUS_ACTIVE)
        self.assertEqual(orphan.egress_id, "")


class WebhookDispatchTest(EgressEventBase):
    """The three events reach the handler through the real dispatch dict."""

    def test_all_three_event_types_are_handled(self):
        for event_type in ("egress_started", "egress_updated", "egress_ended"):
            with self.subTest(event=event_type):
                row = self._row(egress_id=f"EG_{event_type}")
                evt = _event(f"EG_{event_type}", ACTIVE)
                evt.event = event_type
                lv._handle_egress_event(evt)
                row.refresh_from_db()
                self.assertEqual(row.status, LiveSessionEgress.STATUS_ACTIVE)

    def test_handler_is_registered_for_every_egress_event(self):
        """Guards against adding a handler function and forgetting the dict
        entry — the failure mode there is a silently ignored event."""
        import inspect

        src = inspect.getsource(lv.livekit_webhook)
        for event_type in ("egress_started", "egress_updated", "egress_ended"):
            self.assertIn(f'"{event_type}": _handle_egress_event', src)
