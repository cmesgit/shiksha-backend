"""Start LiveKit Egress for a live class, recording into Bunny Edge Storage.

Phase 1 of automatic class recording. This module only STARTS an egress and
records the attempt; nothing yet consumes the mp4 it produces. See the
BUNNY_EGRESS_* block in config/settings_base.py for the credential story and
`LiveSessionEgress` in livestream/models.py for the state machine.

Two decisions in here are load-bearing and easy to undo by accident:

1. **Best-effort, never raising.** This is called off the back of a LiveKit
   participant_joined webhook, on the same path that marks a class LIVE and
   notifies every enrolled student. A recording that fails to start must
   never take the class down with it — the same reasoning as
   `room_admin.close_room`, which this module deliberately mirrors. Every
   failure lands in `LiveSessionEgress.error` instead, which is a durable
   record rather than a log line that has rotated away by the time anyone
   asks why a class has no recording.

2. **The outbound call happens OUTSIDE the caller's transaction.** The
   webhook handler runs under `@transaction.atomic` holding a
   `select_for_update` lock on the LiveSession row; making an HTTP round trip
   to LiveKit while holding that lock would block every concurrent
   join/leave for the duration. Callers therefore hand this to
   `transaction.on_commit`, and the short claim transaction below re-locks
   the session on its own.
"""
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Room-composite layout. "speaker" follows whoever is talking, which for a
# lecture is the teacher almost the whole time; LiveKit's default ("grid")
# would spend most of a 4-person class rendering three idle student tiles at
# the same size as the board being taught from. One-line change if a course
# ever wants grid.
EGRESS_LAYOUT = "speaker"


def storage_key_for(session):
    """Object key inside BUNNY_EGRESS_ZONE for one recording attempt.

    The random segment is a security property, not a uniqueness convenience:
    between egress finishing and the Bunny Stream fetch completing, this
    object has to be readable over a public pull zone by anyone who knows the
    URL (Bunny's POST /videos/fetch cannot read a signed one).
    """
    prefix = (settings.BUNNY_EGRESS_PREFIX or "").strip("/")
    leaf = f"{session.id}/{uuid.uuid4().hex}.mp4"
    return f"{prefix}/{leaf}" if prefix else leaf


def build_request(session, storage_key):
    """The RoomCompositeEgressRequest for one attempt.

    Split out from `start_session_egress` so tests can assert on the exact
    S3 parameters — an endpoint or force_path_style regression here would
    only ever surface as an opaque SigV4 failure against real Bunny.
    """
    from livekit import api as lk_api

    return lk_api.RoomCompositeEgressRequest(
        room_name=session.room_name,
        layout=EGRESS_LAYOUT,
        file_outputs=[
            lk_api.EncodedFileOutput(
                file_type=lk_api.EncodedFileType.MP4,
                filepath=storage_key,
                # LiveKit otherwise writes a sidecar .json manifest next to
                # the mp4. That is a second object to find, serve publicly
                # and purge, for information we already hold in
                # LiveSessionEgress.
                disable_manifest=True,
                s3=lk_api.S3Upload(
                    # Bunny has no separate access-key concept: the Storage
                    # Zone name is the access key AND the bucket, and the
                    # zone password is the secret.
                    access_key=settings.BUNNY_EGRESS_ZONE,
                    secret=settings.BUNNY_EGRESS_API_KEY,
                    bucket=settings.BUNNY_EGRESS_ZONE,
                    region=settings.BUNNY_EGRESS_REGION,
                    # NOT the native Edge Storage host that
                    # config/bunny_storage.py uses — see settings_base.
                    endpoint=f"https://{settings.BUNNY_EGRESS_S3_HOST}",
                    # Bunny's S3 API is path-style; virtual-hosted-style
                    # would put the zone name in the hostname, which its
                    # endpoint does not serve.
                    force_path_style=True,
                ),
            )
        ],
    )


def _start_room_composite(request):
    """The single outbound LiveKit call. Patched wholesale in tests.

    Mirrors room_admin.py's client lifecycle exactly, including closing the
    client in a `finally` — a leaked aiohttp session on a long-lived ASGI
    worker is a slow resource leak rather than an obvious failure.
    """
    from asgiref.sync import async_to_sync
    from livekit import api as lk_api

    async def _run():
        client = lk_api.LiveKitAPI(
            settings.LIVEKIT_URL,
            settings.LIVEKIT_API_KEY,
            settings.LIVEKIT_API_SECRET,
        )
        try:
            return await client.egress.start_room_composite_egress(request)
        finally:
            await client.aclose()

    return async_to_sync(_run)()


def _status_name(raw, default):
    """LiveKit's EgressStatus as the string name LiveSessionEgress stores.

    The SDK hands back a protobuf enum int; storing the name keeps the admin
    readable and survives a renumbering. An unrecognised value falls back
    rather than raising — an SDK that adds a status must not break a class.
    """
    try:
        from livekit.api import EgressStatus

        return EgressStatus.Name(raw)
    except Exception:
        logger.warning("Unrecognised LiveKit EgressStatus: %r", raw)
        return default


def _claim(session):
    """Reserve the right to start an egress for this session, or return the
    attempt that already holds it.

    Serialised on the LiveSession row because callers fire this from
    `on_commit`, i.e. after the webhook handler's own lock is gone: a teacher
    whose client reconnects twice in quick succession produces two
    participant_joined events, and without this both would start a
    (separately billed) egress for one class.
    """
    from livestream.models import LiveSession, LiveSessionEgress

    with transaction.atomic():
        LiveSession.objects.select_for_update().filter(pk=session.pk).first()
        existing = (
            LiveSessionEgress.objects
            .filter(session=session)
            .exclude(status__in=LiveSessionEgress.TERMINAL_STATUSES)
            .first()
        )
        if existing:
            return existing, False
        row = LiveSessionEgress.objects.create(
            session=session, storage_key=storage_key_for(session),
        )
        return row, True


def start_session_egress(session):
    """Begin recording `session`, if recording is configured and not already
    running for it. Returns the LiveSessionEgress row, or None when egress is
    switched off. Never raises.
    """
    if not settings.LIVEKIT_EGRESS_ENABLED:
        return None

    if not session.room_name:
        # Rooms are auto-created by LiveKit on first join and the room name is
        # the only handle egress has; there is nothing to record without it.
        logger.warning("Egress skipped: session %s has no room_name", session.pk)
        return None

    from livestream.models import LiveSessionEgress

    row, claimed = _claim(session)
    if not claimed:
        logger.info(
            "Egress already in flight for session %s (%s, %s)",
            session.pk, row.egress_id or "no id yet", row.status,
        )
        return row

    try:
        info = _start_room_composite(build_request(session, row.storage_key))
    except Exception as exc:
        # Deliberately swallowed — see this module's docstring. The row is the
        # durable record; the class carries on unrecorded.
        logger.exception("Egress start failed for session %s", session.pk)
        row.status = LiveSessionEgress.STATUS_START_FAILED
        row.error = str(exc)[:2000]
        row.save(update_fields=["status", "error"])
        return row

    row.egress_id = getattr(info, "egress_id", "") or ""
    row.status = _status_name(
        getattr(info, "status", None), LiveSessionEgress.STATUS_STARTING,
    )
    row.started_at = timezone.now()
    row.save(update_fields=["egress_id", "status", "started_at"])
    logger.info(
        "Egress %s started for session %s → %s",
        row.egress_id, session.pk, row.storage_key,
    )
    return row


# ── Webhook events ─────────────────────────────────────────────────────────
# Phase 2. LiveKit sends egress_started / egress_updated / egress_ended; all
# three carry the same EgressInfo payload, so one function folds any of them
# into the LiveSessionEgress row and the status field says what happened.

def _ns_to_dt(value):
    """LiveKit reports egress timestamps as unix NANOSECONDS, not seconds.

    Read as seconds these land in 1970 (or ~55,000 years out, depending on
    which way you divide), and the mistake is invisible until someone looks
    at a recording's timeline in the admin.
    """
    from datetime import datetime, timezone as dt_timezone

    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        logger.warning("Unparseable egress timestamp: %r", value)
        return None


def _first_file_result(info):
    """The written file, as LiveKit reports it after the fact.

    `file_results` is the modern field and a repeated one; `file` is the
    deprecated singular. Both are read because which one is populated has
    changed across LiveKit versions and an empty result here silently costs
    phase 3 the object key it needs to fetch.
    """
    results = list(getattr(info, "file_results", None) or [])
    if results:
        return results[0]
    single = getattr(info, "file", None)
    # An unset protobuf message field is present but empty rather than None.
    if single is not None and getattr(single, "filename", "") :
        return single
    return None


def _adopt_orphan(room_name):
    """An egress event whose id we've never seen, matched to a row that is
    still waiting for one.

    `start_session_egress` writes the row BEFORE calling LiveKit precisely so
    a start is never invisible — but that means a response lost in transit
    (timeout, worker restart mid-call) leaves a REQUESTED row with no
    egress_id, while LiveKit really did start a billed egress. These events
    are the only thing that can reconcile the two. Without this, that egress
    records nothing and bills anyway, and the row sits REQUESTED forever
    blocking any retry for the session.
    """
    from livestream.models import LiveSessionEgress

    if not room_name:
        return None
    return (
        LiveSessionEgress.objects
        .select_for_update()
        .filter(session__room_name=room_name, egress_id="")
        .exclude(status__in=LiveSessionEgress.TERMINAL_STATUSES)
        .order_by("-requested_at")
        .first()
    )


def apply_egress_event(event, room_name=""):
    """Fold one egress webhook event into its LiveSessionEgress row.

    Returns the updated row, or None when the event cannot be matched to one.
    Caller supplies `room_name` because resolving it from an egress event is
    the webhook layer's job (see `_event_room_name` in livestream/views.py).
    """
    from livestream.models import LiveSessionEgress

    info = getattr(event, "egress_info", None)
    if info is None:
        logger.warning("Egress event with no egress_info; ignored")
        return None

    egress_id = getattr(info, "egress_id", "") or ""
    row = None
    if egress_id:
        row = (
            LiveSessionEgress.objects
            .select_for_update()
            .filter(egress_id=egress_id)
            .first()
        )
    if row is None:
        row = _adopt_orphan(room_name or getattr(info, "room_name", ""))
        if row is None:
            logger.warning(
                "Egress event for unknown egress %s (room=%r); ignored",
                egress_id or "<no id>", room_name,
            )
            return None
        logger.info("Adopted orphan egress row %s → %s", row.pk, egress_id)
        row.egress_id = egress_id

    fields = {"egress_id"}

    new_status = _status_name(getattr(info, "status", None), row.status)
    # Out-of-order protection. LiveKit retries webhooks and does not promise
    # ordering, so a delayed egress_updated can arrive after egress_ended.
    # The per-event_id dedupe in the webhook sink collapses REDELIVERIES of
    # one event but cannot reorder two different ones, and letting a stale
    # ACTIVE overwrite COMPLETE would put the row back on phase 3's fetch
    # queue after it had already been handled.
    if row.is_terminal and new_status not in LiveSessionEgress.TERMINAL_STATUSES:
        logger.info(
            "Ignoring late %s for terminal egress %s (still %s)",
            new_status, row.egress_id, row.status,
        )
    else:
        row.status = new_status
        fields.add("status")

    started = _ns_to_dt(getattr(info, "started_at", None))
    if started and not row.started_at:
        row.started_at = started
        fields.add("started_at")

    ended = _ns_to_dt(getattr(info, "ended_at", None))
    if ended and not row.ended_at:
        row.ended_at = ended
        fields.add("ended_at")

    err = (getattr(info, "error", "") or "").strip()
    if err:
        row.error = err[:2000]
        fields.add("error")

    result = _first_file_result(info)
    if result is not None:
        # What LiveKit actually wrote wins over what we asked for. They should
        # agree; if they ever don't, phase 3 must fetch the real key.
        filename = (getattr(result, "filename", "") or "").strip()
        if filename and filename != row.storage_key:
            logger.info(
                "Egress %s wrote %r, not the requested %r",
                row.egress_id, filename, row.storage_key,
            )
            row.storage_key = filename[:512]
            fields.add("storage_key")

        size = getattr(result, "size", None)
        if size:
            row.file_size_bytes = int(size)
            fields.add("file_size_bytes")

        # Also nanoseconds, same trap as the timestamps above.
        duration_ns = getattr(result, "duration", None)
        if duration_ns:
            row.duration_seconds = max(0, int(int(duration_ns) / 1_000_000_000))
            fields.add("duration_seconds")

    row.save(update_fields=sorted(fields))
    logger.info(
        "Egress %s → %s (session %s)",
        row.egress_id, row.status, row.session_id,
    )

    # Hand a finished recording to Bunny Stream. on_commit so the worker never
    # reads a row this transaction has not committed yet, and because the
    # caller holds a row lock the fetch's HTTP calls must not sit inside.
    if row.awaiting_stream_fetch:
        pk = row.pk
        transaction.on_commit(
            lambda: _enqueue_stream_fetch(pk)
        )

    return row


def _enqueue_stream_fetch(egress_pk):
    """Queue the Bunny Stream handoff, falling back to running it inline.

    A broker that is down must not mean the recording is silently lost: the
    inline path is slower and blocks the webhook response, but the webhook
    sink already returns 200 regardless of handler outcome, so the worst case
    is a slow request rather than a dropped class recording.
    """
    try:
        from livestream.tasks import fetch_egress_recording

        fetch_egress_recording.delay(egress_pk)
    except Exception:
        logger.exception(
            "Could not queue Bunny Stream fetch for egress %s; running inline",
            egress_pk,
        )
        try:
            hand_off_to_stream(egress_pk)
        except Exception:
            logger.exception("Inline Bunny Stream fetch failed for %s", egress_pk)


# ── Handoff to Bunny Stream ────────────────────────────────────────────────
# Phase 3. The mp4 exists in Bunny Storage; this turns it into the
# SessionRecording the rest of the app already knows how to play.

# A fetch that has failed this many times is a configuration problem, not a
# transient one. Retrying forever would hammer Bunny and hide the real fault.
MAX_FETCH_ATTEMPTS = 5


def _claim_for_fetch(egress_pk):
    """Take exclusive ownership of one handoff, or decline.

    Returns the row, or None when someone else already handled it. Locked
    because both the egress_ended webhook and (from phase 4) a sweep can
    reach the same row: without this, two workers create two Bunny Stream
    videos for one class and one of them is billed forever with nothing
    pointing at it.
    """
    from livestream.models import LiveSessionEgress

    with transaction.atomic():
        row = (
            LiveSessionEgress.objects
            .select_for_update()
            .select_related("session", "session__subject")
            .filter(pk=egress_pk)
            .first()
        )
        if row is None:
            return None
        if row.recording_id is not None:
            logger.info("Egress %s already has recording %s", row.pk,
                        row.recording_id)
            return None
        if not row.awaiting_stream_fetch:
            logger.info(
                "Egress %s not ready for fetch (status=%s, key=%r)",
                row.pk, row.status, row.storage_key,
            )
            return None
        if row.fetch_attempts >= MAX_FETCH_ATTEMPTS:
            logger.error(
                "Egress %s exhausted %s fetch attempts; giving up",
                row.pk, MAX_FETCH_ATTEMPTS,
            )
            return None
        row.fetch_attempts += 1
        row.save(update_fields=["fetch_attempts"])
        return row


def hand_off_to_stream(egress_pk):
    """Pull a finished egress recording into Bunny Stream as a
    SessionRecording. Idempotent, and safe to call twice.

    Ordering here is deliberate: the Bunny video slot is created, then the
    SessionRecording row, and only then is the fetch requested. Creating the
    row before the fetch means a crash mid-handoff leaves a VISIBLE
    unpublished recording a teacher and an admin can both see (the faculty
    grid shows unpublished rows as Pending), rather than an invisible Bunny
    video that bills forever with nothing in this database pointing at it.
    A retry re-issues the fetch against the same guid, which is harmless.
    """
    from courses.models_recordings import SessionRecording
    from livestream.services import bunny_stream

    row = _claim_for_fetch(egress_pk)
    if row is None:
        return None

    session = row.session
    url = bunny_stream.public_url_for(row.storage_key)
    if not url:
        # Misconfiguration, not a transient fault: no amount of retrying
        # invents a pull zone. Recorded so it shows up in the admin.
        msg = ("BUNNY_EGRESS_PULL_HOST is not set — Bunny Stream cannot fetch "
               "the recording. See config/settings_base.py.")
        logger.error("Egress %s: %s", row.pk, msg)
        row.error = msg
        row.fetch_attempts = MAX_FETCH_ATTEMPTS
        row.save(update_fields=["error", "fetch_attempts"])
        return None

    try:
        guid = bunny_stream.create_video_slot(session.title)
    except Exception as exc:
        logger.exception("Egress %s: Bunny Stream slot creation failed", row.pk)
        row.error = str(exc)[:2000]
        row.save(update_fields=["error"])
        return None

    recording = SessionRecording.objects.create(
        subject=session.subject,
        # Inherited from the class, exactly as SaveRecordingView does for a
        # manual upload: the batch that attended sees its own recording.
        batch=session.batch,
        live_session=session,
        title=session.title,
        description="Automatically recorded live class.",
        session_date=session.start_time.date(),
        bunny_video_id=guid,
        # No human uploaded this. The column was made nullable in phase 0
        # precisely for this row.
        uploaded_by=None,
        status=0,  # Created; Bunny advances it, phase 4 polls for it.
        # Deliberately unpublished until Bunny reports status 4. Students
        # filter on is_published, teachers do not, so the faculty grid shows
        # it as Pending while it transcodes and nobody is offered a video
        # that cannot play yet. Phase 4 publishes and notifies.
        is_published=False,
    )
    row.recording = recording
    row.save(update_fields=["recording"])

    try:
        bunny_stream.fetch_into_video(guid, url)
    except Exception as exc:
        # The recording row survives on purpose — see this function's
        # docstring. A retry finds it via `recording_id` and re-issues.
        logger.exception("Egress %s: Bunny Stream fetch failed", row.pk)
        row.error = str(exc)[:2000]
        row.save(update_fields=["error"])
        return recording

    logger.info(
        "Egress %s handed off: %s → Bunny Stream video %s (recording %s)",
        row.pk, row.storage_key, guid, recording.pk,
    )
    return recording


# ── Finishing a recording ──────────────────────────────────────────────────
# Phase 4. Bunny has transcoded; publish the recording, tell the students, and
# get the raw mp4 off the public pull zone.

# How far back the sweep looks. Beyond this an attempt needs a human, not
# another retry, and an unbounded scan would grow with every class ever held.
SWEEP_WINDOW_DAYS = 7


def _notify_recording_available(recording):
    """Tell the students who can actually see this recording that it exists.

    Deliberately here and not at fetch time: until Bunny reports status 4 the
    video cannot play, so notifying earlier promises something the recording
    cannot yet deliver. Reuses the exact path SaveRecordingView uses for a
    manual upload, so an automatic recording produces the same bell, Activity
    row and WS frame as a teacher's own.

    Best-effort: a notification backend hiccup must not stop the recording
    being published or the raw file being purged.
    """
    try:
        from activity.models import Activity
        from activity.signals import _bulk_notify_students, _enrollments_for

        subject = recording.subject
        _bulk_notify_students(
            _enrollments_for(subject.course, recording.batch_id),
            recording,
            Activity.TYPE_RECORDING,
            f"New recording: {recording.title}",
            None,
            subject.id,
            subject.name,
            verb="recording.uploaded",
            link_url=f"/subjects/recordings/{subject.id}",
        )
    except Exception as e:
        logger.warning(
            "Recording notification failed for %s: %s", recording.pk, e)


def finish_recording(row):
    """Publish a transcoded recording and purge its raw mp4.

    Split from the sweep so it is callable directly (an admin retry, a test)
    and so the sweep's loop stays readable. Returns True when the recording
    reached its final published state on this call.
    """
    from django.utils import timezone as dj_tz
    from livestream.services import bunny_stream

    recording = row.recording
    if recording is None:
        return False

    if recording.status != 4:
        return False

    newly_published = not recording.is_published
    if newly_published:
        recording.is_published = True
        recording.save(update_fields=["is_published"])
        logger.info("Published automatic recording %s", recording.pk)
        _notify_recording_available(recording)

    # Purge last, and independently of publishing: if the delete fails the
    # recording is still watchable, and raw_deleted_at staying NULL means the
    # next sweep tries again. The reverse order would risk a published
    # recording whose source was deleted before Stream had really ingested it.
    if row.raw_deleted_at is None and row.storage_key:
        try:
            bunny_stream.delete_raw_object(row.storage_key)
        except Exception as exc:
            logger.exception(
                "Could not purge raw object for egress %s", row.pk)
            row.error = str(exc)[:2000]
            row.save(update_fields=["error"])
            return newly_published
        row.raw_deleted_at = dj_tz.now()
        row.save(update_fields=["raw_deleted_at"])

    return newly_published


def sweep_recordings():
    """Advance every in-flight automatic recording. Safe to run repeatedly.

    Three jobs, in the order they matter:

    1. Drain the Bunny Stream fetch queue. This is the backstop for a LOST
       egress_ended webhook — without it the mp4 would sit in Storage forever,
       publicly readable, because the webhook is otherwise the only trigger.
    2. Poll Bunny for recordings still transcoding. Nothing else does: a
       teacher's upload has a browser polling CheckVideoStatusView, an
       automatic recording has nobody watching it.
    3. Publish and purge whatever has finished.

    Returns a small counts dict, which is what the Celery result and the logs
    show — a sweep that silently does nothing is indistinguishable from a
    sweep that is not running.
    """
    from datetime import timedelta

    from django.utils import timezone as dj_tz

    from courses.services_recordings import refresh_from_bunny
    from livestream.models import LiveSessionEgress

    since = dj_tz.now() - timedelta(days=SWEEP_WINDOW_DAYS)
    counts = {"fetched": 0, "polled": 0, "finished": 0}

    # 1. Missed-webhook backstop.
    stalled = (
        LiveSessionEgress.objects
        .filter(
            status=LiveSessionEgress.STATUS_COMPLETE,
            recording__isnull=True,
            requested_at__gte=since,
            fetch_attempts__lt=MAX_FETCH_ATTEMPTS,
        )
        .exclude(storage_key="")
    )
    for row in stalled:
        if hand_off_to_stream(row.pk) is not None:
            counts["fetched"] += 1

    # 2 & 3. Poll and finish.
    pending = (
        LiveSessionEgress.objects
        .select_related("recording", "recording__subject",
                        "recording__subject__course")
        .filter(recording__isnull=False, requested_at__gte=since)
        .exclude(recording__status__in=[4, 5])
    )
    for row in pending:
        try:
            if refresh_from_bunny(row.recording):
                counts["polled"] += 1
            if finish_recording(row):
                counts["finished"] += 1
        except Exception:
            # One bad recording must not abort the sweep for the others.
            logger.exception("Sweep failed for egress %s", row.pk)

    # A recording that reached status 4 in an earlier sweep but whose purge
    # failed is excluded by the status filter above, so it is picked up here.
    for row in (
        LiveSessionEgress.objects
        .select_related("recording", "recording__subject")
        .filter(recording__isnull=False, recording__status=4,
                raw_deleted_at__isnull=True, requested_at__gte=since)
        .exclude(storage_key="")
    ):
        try:
            finish_recording(row)
        except Exception:
            logger.exception("Purge retry failed for egress %s", row.pk)

    if any(counts.values()):
        logger.info("Egress sweep: %s", counts)
    return counts
