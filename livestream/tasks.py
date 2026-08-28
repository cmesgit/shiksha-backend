# PLACEMENT: backend/backend/livestream/tasks.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/livestream/tasks.py
#
# SUPERSEDES the set-1 tasks.py. Includes everything from set 1
# (notification group → user_updates_<id>, duplicate task removed) PLUS:
#
# NEW: sync_open_session_statuses() — a 1-minute sweep that calls
# session.sync_status() on every non-terminal session, so the reconnection
# ladder (RECONNECTING → PAUSED → COMPLETED) advances on a TIMER and the stored
# `status` column stops drifting from what computed_status() displays. It
# broadcasts only on a real transition. Wire it into beat at */1 min.
#
# The old auto_complete_expired_sessions is kept (end_time / abandoned cleanup)
# but now delegates the actual flip to sync_status() so the two never disagree
# on HOW a session becomes COMPLETED.

import logging

from config.celery import app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=5)
def push_ws_notification_task(self, user_id, data):
    """Async WS notification push. Targets user_updates_<id> (the group
    accounts.consumers.UserUpdateConsumer joins). Retries up to 3×."""
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f"user_updates_{user_id}",
            {"type": "send_notification", "data": data},
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task
def sync_open_session_statuses():
    """Advance the live-session state machine on a timer.

    For every non-terminal LiveSession, persist computed_status() to the stored
    column via sync_status(). This makes the reconnection ladder
    (RECONNECTING→PAUSED→COMPLETED) fire at the real time boundary instead of
    only when someone reads the session, and keeps raw `status=` filters in
    sync with the displayed status. Broadcasts only on an actual change.

    Schedule (config/celery.py beat):
        "sync-open-session-statuses": {
            "task": "livestream.tasks.sync_open_session_statuses",
            "schedule": crontab(minute="*/1"),
        },
    """
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    open_qs = LiveSession.objects.exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    changed = 0
    for session in open_qs:
        did_change, _ = session.sync_status(save=True)
        if did_change:
            changed += 1
            try:
                broadcast_session_update(session)
            except Exception:
                pass

    return {"transitioned": changed}


def _livekit_room_identities(room_name):
    """Return the set of participant identities LiveKit reports for a room, or
    None if LiveKit is unreachable / not configured / the room doesn't exist.
    Identity == str(user.id) (see services/token.py). Best-effort: any failure
    falls back to our own durable attendance data upstream."""
    try:
        from django.conf import settings
        from asgiref.sync import async_to_sync
        from livekit import api as lk_api

        async def _fetch():
            client = lk_api.LiveKitAPI(
                settings.LIVEKIT_URL,
                settings.LIVEKIT_API_KEY,
                settings.LIVEKIT_API_SECRET,
            )
            try:
                resp = await client.room.list_participants(
                    lk_api.ListParticipantsRequest(room=room_name)
                )
                return {p.identity for p in resp.participants}
            finally:
                await client.aclose()

        return async_to_sync(_fetch)()
    except Exception:
        return None


@app.task
def sample_live_viewers():
    """Every minute: snapshot concurrent viewers for open sessions and reconcile
    missed leaves.

    For each live/open session:
      • If LiveKit is reachable, use its authoritative participant list to (a)
        close attendance intervals for users no longer in the room (catches a
        dropped participant_left webhook), and (b) record the viewer count.
      • Otherwise fall back to our durable open-interval count.
    Updates LiveSession.peak_viewers and writes a LiveSessionViewerSample.

    Schedule (config/celery.py beat): crontab(minute="*/1").
    """
    from django.utils import timezone
    from livestream.models import LiveSession, LiveSessionViewerSample
    from livestream.services import attendance as attendance_svc

    now = timezone.now()
    open_qs = LiveSession.objects.filter(
        status__in=[
            LiveSession.STATUS_LIVE,
            LiveSession.STATUS_RECONNECTING,
            LiveSession.STATUS_WAITING,
            LiveSession.STATUS_PAUSED,
        ],
        end_time__gte=now,
    )

    sampled = 0
    for session in open_qs:
        identities = _livekit_room_identities(session.room_name)

        if identities is not None:
            # Reconcile: anyone with an open interval but absent from the room
            # left without a webhook — close their interval defensively.
            from livestream.models import LiveSessionAttendanceInterval
            # (user, profile) pairs — two children of one parent in the
            # same class are two attendees, and reconciling by user alone
            # would evict both when only one had left.
            open_pairs = (
                LiveSessionAttendanceInterval.objects
                .filter(session=session, left_at__isnull=True)
                .values_list("user_id", "learner_profile_id")
                .distinct()
            )
            # Parse before comparing. LiveKit identities are composite
            # ("{user}_{session}") while open_user_ids are bare user ids, so a
            # raw comparison matches NOTHING — and this sweep runs every
            # minute, so it would close every open interval for everyone
            # actually sitting in the class, wiping their attendance and
            # zeroing the live viewer count. parse_identity also accepts the
            # legacy bare form, which is what participants who joined before
            # the composite change still hold.
            from livestream.services.token import parse_identity
            from livestream.services.token import parse_profile_id
            present = {
                (parse_identity(i), parse_profile_id(i)) for i in identities
            }
            for uid, pid in list(open_pairs):
                key = (str(uid), str(pid) if pid else None)
                if key not in present:
                    from django.contrib.auth import get_user_model
                    u = get_user_model().objects.filter(id=uid).first()
                    if u:
                        attendance_svc.close_intervals(
                            session, u, when=now, reconcile=True,
                            learner_profile=pid)
            count = len(identities)
        else:
            count = attendance_svc.current_watching(session)

        LiveSessionViewerSample.objects.create(session=session, viewers=count)
        if count > session.peak_viewers:
            session.peak_viewers = count
            session.save(update_fields=["peak_viewers"])
        sampled += 1

    return {"sampled": sampled}


@app.task
def auto_complete_expired_sessions():
    """Safety-net cleanup (every 5 min): close sessions past end_time or with a
    teacher gone > 60 min. Delegates the flip to sync_status() so the ladder
    and this task agree on how a session reaches COMPLETED.
    """
    from django.utils import timezone
    from datetime import timedelta
    from livestream.models import LiveSession
    from livestream.views import broadcast_session_update

    now = timezone.now()
    completed_count = 0

    # Respect the overrun grace and any teacher-granted extension, so this
    # sweep cannot force-end a class that is legitimately still running.
    # computed_status() already refuses to complete inside the grace, so
    # filtering here is really about not churning through live sessions every
    # five minutes — but keeping the two in agreement is the point: they
    # disagreeing about what "over" means is how end_time became a hard kill
    # in three places at once.
    from django.db.models.functions import Coalesce

    candidates = LiveSession.objects.annotate(
        effective_end=Coalesce("extended_until", "end_time"),
    ).filter(
        effective_end__lte=now - LiveSession.LIVE_GRACE,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )
    abandoned_cutoff = now - timedelta(minutes=60)
    abandoned = LiveSession.objects.filter(
        teacher_left_at__lte=abandoned_cutoff,
    ).exclude(
        status__in=[LiveSession.STATUS_COMPLETED, LiveSession.STATUS_CANCELLED]
    )

    seen = set()
    for session in list(candidates) + list(abandoned):
        if session.pk in seen:
            continue
        seen.add(session.pk)
        # end_time / abandoned both resolve to COMPLETED via computed_status();
        # sync_status persists it and clears the reconnect timer.
        did_change, new_status = session.sync_status(save=True)
        # NOTE: there used to be a "defensive" force-complete here, guarded by
        # `if session.status != COMPLETED`. It was unreachable: sync_status()
        # has already assigned session.status = new_status by this point, so
        # inside `new_status == COMPLETED` the guard is always false. It read
        # as a safety net that had never once run — worse than no safety net,
        # because it stopped anyone looking for a real one. sync_status is the
        # single path to COMPLETED and is trusted as such.
        if session.status == LiveSession.STATUS_COMPLETED:
            # A session that ends on the clock used to flip this column and
            # nothing else — unlike every other end path. Two consequences,
            # both invisible until someone complained:
            #
            #   * the LiveKit room stayed open, so everyone still connected
            #     kept publishing media for up to their 2h token TTL. The UI
            #     said the class was over while the call carried on, and the
            #     minutes were still being billed.
            #   * attendance intervals stayed open, and because this task is
            #     the last thing to touch the session, no sweep ever repaired
            #     them (sample_live_viewers only scans non-terminal statuses).
            #     duration_seconds() reads an open interval as 0, so students
            #     silently lost the entire class from their attendance.
            #
            # Both calls are idempotent and must not be able to stop the loop:
            # one unreachable LiveKit room should not leave the rest of the
            # batch un-swept.
            try:
                from livestream.services import attendance as attendance_svc
                attendance_svc.close_all_open(session, when=now)
            except Exception:
                logger.exception("auto_complete: attendance close failed (session=%s)",
                                 session.pk)
            try:
                from livestream.services.room_admin import close_room
                if session.room_name:
                    close_room(session.room_name)
            except Exception:
                logger.exception("auto_complete: close_room failed (session=%s)",
                                 session.pk)
            if session.actual_ended_at is None:
                # Left NULL forever on this path, which defeated the
                # true-live-duration analytics the field exists for.
                session.actual_ended_at = now
                session.save(update_fields=["actual_ended_at"])

        if did_change:
            completed_count += 1
            try:
                broadcast_session_update(session)
            except Exception:
                pass

    return f"Auto-completed/advanced {completed_count} sessions"


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_egress_recording(self, egress_pk):
    """Pull one finished egress recording into Bunny Stream (phase 3).

    Thin wrapper: all the logic, including its own idempotence claim, lives in
    livestream/services/egress.py::hand_off_to_stream. Retries are for
    transient Bunny/network failures only — the service's own fetch_attempts
    counter is the real backstop, so an exhausted row is a no-op rather than
    an error loop.
    """
    from livestream.services.egress import hand_off_to_stream

    try:
        hand_off_to_stream(egress_pk)
    except Exception as exc:
        logger.exception("fetch_egress_recording failed for %s", egress_pk)
        raise self.retry(exc=exc)
