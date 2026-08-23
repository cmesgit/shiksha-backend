import json
from django.conf import settings
from .serializers import (
    LiveSessionCreateSerializer,
    RecurringLiveSessionSerializer,
    LiveSessionUpdateSerializer,
    LiveSessionListSerializer,
    SessionReviewSerializer,
    SessionNoteSerializer,
)
from .services.token import (
    generate_livekit_token, build_identity, parse_identity, parse_profile_id,
)
from .services.room_admin import close_room
from .services import attendance as attendance_svc
from .models import (
    LiveSession,
    LiveSessionAttendance,
    LiveSessionRemoval,
    LiveKitWebhookEvent,
    SessionReview,
    SessionNote,
)
from courses.board_display import board_name_via
from courses.services import teaches_subject
from accounts.permissions import require_teacher_context, IsTeacherContext, CTX_TEACHER
from enrollments.models import Enrollment
from livekit.api import WebhookReceiver, TokenVerifier
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from django.db.models import Q
from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from livestream.services.session_state import set_session_state


logger = logging.getLogger(__name__)


# =========================
# BROADCAST HELPERS
# =========================

def broadcast_session_update(session):
    """Broadcast status change to everyone inside the session room."""
    channel_layer = get_channel_layer()

    # Update Redis state cache (safe — never breaks if Redis is down)
    try:
        set_session_state(session)
    except Exception:
        pass

    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"session_{session.id}",
        {
            "type": "session_update",
            "data": {
                "status": session.computed_status(),
                "teacher_left_at": (
                    session.teacher_left_at.isoformat()
                    if session.teacher_left_at else None
                ),
            },
        },
    )

    # Also notify the session list page
    broadcast_course_sessions_update(session)


def broadcast_course_sessions_update(session):
    """Broadcast session changes to the session list page (LiveSessions.jsx).

    Sends the full LiveSessionListSerializer payload (not a hand-rolled thin
    dict) so both the student and teacher cards get computed_status/
    subject_name/course_name/description without a REST refetch.

    CAVEAT: this runs outside any DRF request, so
    LiveSessionListSerializer.get_can_join()'s `request.user.has_role("TEACHER")`
    branch never fires here — the pushed `can_join` always falls through to
    the generic student timing rule. Teacher frontends must not trust this
    field over the socket; they recompute a teacher-safe can_join client-side
    instead.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    data = dict(LiveSessionListSerializer(session).data)

    async_to_sync(channel_layer.group_send)(
        f"course_sessions_{session.course_id}",
        {
            "type": "session_list_update",
            "data": data,
        }
    )


# =========================
# STUDENT SESSION LIST
# =========================

class StudentLiveSessionListView(generics.ListAPIView):
    serializer_class = LiveSessionListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        # Do NOT hard-require the STUDENT *role* here. A teacher account browsing
        # in learner context has a SELF learner profile but no STUDENT role (see
        # signup_serializer._setup_teacher, which never adds it), and the sibling
        # learner lists — private student_sessions and group my_group_sessions —
        # don't gate on the role either; they scope by ownership/enrollment.
        # Gating live sessions on has_role("STUDENT") 403'd those users, which the
        # UI surfaced as "Unable to load sessions" while private & group loaded
        # fine. Access is already correctly scoped by the active-enrollment filter
        # below, so a user with no active enrollment just gets an empty list
        # instead of an error.

        course_id = self.request.query_params.get("course_id")
        subject_id = self.request.query_params.get("subject_id")

        # PROFILE-scoped, not account-scoped. Keyed on `user=` this collected
        # every sibling's enrolments, so their batch ids landed in
        # active_batch_ids below and child A saw child B's batch timetable —
        # the comment right underneath claimed a correctness the code did not
        # implement.
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(self.request)
        if learner is None:
            return LiveSession.objects.none()
        active_enrollments = Enrollment.objects.filter(
            learner_profile=learner,
            status=Enrollment.STATUS_ACTIVE
        )
        active_courses = active_enrollments.values_list("course_id", flat=True)
        # Batches the student belongs to. A session is visible if it is
        # course-wide (batch IS NULL) or belongs to one of these batches, so a
        # morning batch never sees the evening batch's timetable. A student
        # with no batch assigned sees only course-wide sessions (safe).
        active_batch_ids = list(
            active_enrollments.exclude(batch__isnull=True)
            .values_list("batch_id", flat=True)
        )

        queryset = (
            LiveSession.objects
            .filter(course_id__in=active_courses)
            .filter(Q(batch__isnull=True) | Q(batch_id__in=active_batch_ids))
            .select_related("course__board", "subject", "created_by")
        )

        if course_id:
            queryset = queryset.filter(course_id=course_id)

        if subject_id:
            queryset = queryset.filter(subject_id=subject_id)

        now = timezone.now()
        cutoff = now - timedelta(hours=24)
        queryset = queryset.filter(end_time__gte=cutoff)

        return queryset.order_by("start_time")


# =========================
# TEACHER SESSION LIST
# =========================

class TeacherLiveSessionListView(generics.ListAPIView):
    serializer_class = LiveSessionListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.request.query_params.get("subject_id")

        now = timezone.now()
        cutoff = now - timedelta(days=90)

        if subject_id:
            if not user.teaching_assignments.filter(
                subject_id=subject_id, is_active=True,
            ).exists():
                raise PermissionDenied("Not assigned to this subject.")

            return (
                LiveSession.objects
                .filter(subject_id=subject_id)
                .filter(end_time__gte=cutoff)
                .select_related("course__board", "subject", "created_by")
                .order_by("start_time")
            )

        assigned_subject_ids = user.teaching_assignments.filter(
            is_active=True,
        ).values_list("subject_id", flat=True)

        cutoff = now - timedelta(days=90)
        return (
            LiveSession.objects
            .filter(subject_id__in=assigned_subject_ids)
            .filter(end_time__gte=cutoff)
            .select_related("course__board", "subject", "created_by")
            .order_by("start_time")
        )


# =========================
# JOIN SESSION
# =========================

from django_ratelimit.decorators import ratelimit
from django.utils.decorators import method_decorator

@api_view(["POST"])
@permission_classes([IsAuthenticated])
# 30/min, not 10. Each reconnect costs one call, and a student on flaky mobile
# data can legitimately burn ten in a minute — at which point they were locked
# out of the rest of the class with a bare 403 and no explanation, which looks
# identical to being banned. Still low enough to bound token minting.
@ratelimit(key="user", rate="30/m", method="POST", block=True)
def join_live_session(request, session_id):
    user = request.user
    # select_related: the response below reads course/subject/batch names.
    session = get_object_or_404(
        LiveSession.objects.select_related("course__board", "subject", "batch"),
        id=session_id,
    )
    now = timezone.now()

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session was cancelled."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session has ended."}, status=400)

    # hard_end_time, not end_time — a class routinely runs a few minutes past
    # its planned end, and this gate rejecting at end_time meant a student who
    # briefly dropped could not rejoin a lesson that was visibly still running
    # for everyone else. The teacher can push this out further via /extend/.
    if now >= session.hard_end_time:
        session.status = LiveSession.STATUS_COMPLETED
        session.save(update_fields=["status"])
        return Response({"detail": "Session has ended."}, status=400)

    # A removed participant must not be handed a fresh token. LiveKit tokens
    # are bearer credentials with no revocation, so without this check the
    # teacher's "Remove" only disconnected them for as long as it took to hit
    # refresh. Teachers are exempt so a mis-click cannot lock a substitute out
    # of the class they are meant to be teaching.
    if not (bool(getattr(request, "auth", None))
            and request.auth.get("context") == CTX_TEACHER
            and user.has_role("TEACHER")):
        if LiveSessionRemoval.objects.filter(
            session=session, user=user, revoked_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "You were removed from this class by the teacher."},
                status=403,
            )

    if session.teacher_left_at:
        diff = now - session.teacher_left_at
        if diff > timedelta(minutes=60):
            session.status = LiveSession.STATUS_COMPLETED
            session.teacher_left_at = None
            session.save(update_fields=["status", "teacher_left_at"])
            return Response({"detail": "Session has ended."}, status=400)

    # Branch on the JWT `context` claim, not `has_role` — a teacher account
    # browsing in learner context (e.g. its own SELF learner profile) has no
    # STUDENT role (see signup_serializer._setup_teacher, which never adds
    # it), so gating on has_role("STUDENT") sent it down the has_role("TEACHER")
    # branch instead and handed out a PRESENTER token. Same class of bug
    # already fixed in StudentLiveSessionListView.get_queryset above; every
    # other teacher-only endpoint in this file already gates on context via
    # require_teacher_context, so this mirrors that convention.
    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )

    # Set by the learner branch; stays None for a teacher, who has no
    # learner profile. Declared here so the token call below cannot depend on
    # which branch happened to run.
    joining_profile = None

    # ── Teacher ──
    if in_teacher_context:
        if not teaches_subject(user, session.subject):
            return Response({"detail": "Not assigned"}, status=403)

        is_creator = str(session.created_by_id) == str(user.id)
        # Presenter rights follow the teaching assignment, not authorship —
        # we are already inside the teaches_subject() guard above. Previously
        # this was `is_teacher = is_creator`, so a substitute covering the
        # class was handed the viewer grant and could talk but not show their
        # camera or share slides. See _teacher_may_control().
        is_teacher = True

        # Revive session if teacher reconnects within 60 min — matches the
        # session-completed cutoff a few lines up (line 241), the student
        # cutoff below (line 299), and computed_status()'s own PAUSED window
        # (models.py). This was drifted to 30 independently: a teacher
        # reconnecting 30-60 min after leaving still passed the outer
        # `diff > 60min` check and reached this branch, but this narrower
        # window silently skipped reviving teacher_left_at/status, so
        # computed_status() kept reporting PAUSED/RECONNECTING even though
        # the teacher already had a working LiveKit token from below.
        if is_creator and session.teacher_left_at:
            if now <= session.teacher_left_at + timedelta(minutes=60):
                session.teacher_left_at = None
                session.status = LiveSession.STATUS_LIVE
                session.save(update_fields=["teacher_left_at", "status"])

    # ── Student / learner context ──
    else:
        from enrollments.services import has_active_subscription, lock_payload
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(request)
        joining_profile = learner
        if learner is None:
            return Response(
                {"detail": "Select a learner profile to join.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        if not has_active_subscription(user=user, course=session.course, learner_profile=learner):
            return Response(lock_payload(user=user, course=session.course, learner_profile=learner), status=402)

        # BATCH GATE. Subscription proves they paid for the COURSE; it says
        # nothing about which cohort's class this is. Without this a
        # morning-batch learner who knew (or guessed) a session id was minted
        # a real LiveKit token for the evening batch's live class.
        if not _learner_may_access_session(learner, session):
            return Response(
                {"detail": "This session is not available to your batch."},
                status=403,
            )

        # Recheck subscription hasn't expired or been revoked mid-session
        if session.status in [LiveSession.STATUS_LIVE, LiveSession.STATUS_PAUSED]:
            if not has_active_subscription(user=user, course=session.course, learner_profile=learner):
                return Response(lock_payload(user=user, course=session.course, learner_profile=learner), status=402)

        if now < session.start_time - timedelta(minutes=15):
            return Response({"detail": "Too early"}, status=403)

        if session.teacher_left_at:
            if now - session.teacher_left_at > timedelta(minutes=60):
                return Response({"detail": "Session ended"}, status=403)

        is_teacher = False

    token = generate_livekit_token(
        user=user,
        session=session,
        is_teacher=is_teacher,
        # Baked into the LiveKit identity so the attendance webhooks can
        # tell WHICH child was in the room. None in teacher context.
        learner_profile=joining_profile,
    )

    # Identify the session in the response. Both room pages (teacher and
    # student) call ONLY this endpoint, so without these the room had nothing
    # to render but a hardcoded "Live Session" — a host could not tell which
    # batch, or even which course, they were about to teach. The teacher-only
    # /detail/ endpoint can't fill the gap: it 403s for students.
    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": "PRESENTER" if is_teacher else "STUDENT",
        "title": session.title,
        "subject_name": session.subject.name,
        "course_name": session.course.title,
        "board_name": board_name_via(session, "course"),
        "batch_name": session.batch.name if session.batch_id else None,
    })


# =========================
# CREATE SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_live_session(request):
    require_teacher_context(request)

    serializer = LiveSessionCreateSerializer(
        data=request.data,
        context={"request": request}
    )

    if serializer.is_valid():
        session = serializer.save()
        broadcast_course_sessions_update(session)
        return Response(
            {
                "id": session.id,
                "room": session.room_name,
                "status": session.status,
            },
            status=status.HTTP_201_CREATED
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_recurring_live_sessions(request):
    """Create a whole term's classes from one repeating pattern.

    Returns what was created AND what was skipped, with reasons — a bulk
    tool that silently drops dates is worse than no bulk tool, because the
    teacher believes the timetable is complete.
    """
    require_teacher_context(request)

    serializer = RecurringLiveSessionSerializer(
        data=request.data, context={"request": request})
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    result = serializer.save()
    created = result["created"]

    if created:
        # One broadcast for the batch, not one per session — 50 identical
        # refresh nudges would be its own denial of service on the client.
        broadcast_course_sessions_update(created[0])

    return Response({
        "series_id": str(result["series_id"]),
        "created_count": len(created),
        "skipped_count": len(result["skipped"]),
        "sessions": [{"id": str(s.id), "start_time": s.start_time,
                      "end_time": s.end_time} for s in created],
        "skipped": [{"start_time": s["start_time"], "reason": s["reason"]}
                    for s in result["skipped"]],
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_live_session_series(request, series_id):
    """Cancel the remaining classes in a series.

    The counterpart to bulk creation: a teacher who generated 50 classes and
    then had the batch rescheduled must not have to cancel them one by one.
    Only FUTURE, still-open sessions are touched — past and in-progress
    classes keep their record.
    """
    user = request.user
    require_teacher_context(request)

    qs = LiveSession.objects.filter(series_id=series_id)
    first = qs.first()
    if first is None:
        return Response({"detail": "No such series."}, status=404)
    if not _teacher_may_control(user, first):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    cancelled = qs.filter(
        start_time__gt=timezone.now(),
    ).exclude(
        status__in=[LiveSession.STATUS_CANCELLED, LiveSession.STATUS_COMPLETED],
    ).update(status=LiveSession.STATUS_CANCELLED)

    broadcast_course_sessions_update(first)
    return Response({"detail": f"Cancelled {cancelled} upcoming classes.",
                     "cancelled_count": cancelled})


# =========================
# CANCEL SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_live_session(request, session_id):
    user = request.user
    # Role/context check first: a non-teacher gets the same 403 whether or
    # not session_id exists, instead of a 404-vs-403 split that would leak
    # which UUIDs are real sessions.
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if session.created_by != user:
        return Response({"detail": "You can only cancel your own sessions."}, status=403)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session is already cancelled."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Cannot cancel a completed session."}, status=400)

    # A teacher can join (and so move status to WAITING_FOR_TEACHER/LIVE)
    # before session.start_time — join_live_session has no time gate on the
    # teacher branch. The old check here only looked at wall-clock time
    # against start_time, so an early-joined, genuinely-live session with
    # students already connected could still be "cancelled" out from under
    # them. Gate on status instead: cancel is only for a session nobody has
    # joined yet.
    if session.status != LiveSession.STATUS_SCHEDULED:
        return Response({"detail": "Cannot cancel a session that has already started. Use End instead."}, status=400)

    session.status = LiveSession.STATUS_CANCELLED
    session.save(update_fields=["status"])
    broadcast_course_sessions_update(session)

    return Response({"detail": "Session cancelled successfully."})


# =========================
# RESCHEDULE / EDIT SESSION
# =========================

@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def reschedule_live_session(request, session_id):
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if session.created_by != user:
        return Response({"detail": "You can only edit your own sessions."}, status=403)

    if session.status in (LiveSession.STATUS_CANCELLED, LiveSession.STATUS_COMPLETED):
        return Response({"detail": "This session has already ended."}, status=400)

    if timezone.now() >= session.start_time:
        return Response({"detail": "Cannot edit a session that has already started."}, status=400)

    serializer = LiveSessionUpdateSerializer(
        session, data=request.data, partial=True, context={"request": request}
    )
    if serializer.is_valid():
        serializer.save()
        broadcast_course_sessions_update(session)
        return Response(LiveSessionListSerializer(session, context={"request": request}).data)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _teacher_may_control(user, session):
    """Who may present, extend, moderate and end this class.

    This was `session.created_by == user` at every call site, which produced
    a teacher who could enter the room but not teach in it: entry is gated on
    teaches_subject(), so a substitute got in fine, then received the VIEWER
    grant — can_publish_sources=["microphone"], i.e. mic only, no camera and
    no screen share — and a 403 from End. Over a 6-12 month course, cover for
    illness and staff changes is a certainty, so control follows the same
    teaching assignment that already governs entry.

    Note this is deliberately NOT used for the reconnect-revive logic, which
    stays creator-only: reviving is about the person whose disconnect paused
    the class, not about who is allowed to run it.
    """
    return teaches_subject(user, session.subject)


# =========================
# EXTEND SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def extend_live_session(request, session_id):
    """Push back the end of a class that is running long.

    Livestream had no such endpoint (group sessions did), so end_time was an
    immovable wall: a class overrunning by ten minutes was marked COMPLETED
    underneath everyone, and any student who reconnected was told the session
    had ended while the lesson was visibly still going.
    """
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if not _teacher_may_control(user, session):
        return Response({"detail": "Not assigned to this subject."}, status=403)
    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session was cancelled."}, status=400)
    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session has ended."}, status=400)

    try:
        minutes = int(request.data.get("minutes", 15))
    except (TypeError, ValueError):
        return Response({"detail": "minutes must be a whole number."}, status=400)
    if minutes <= 0:
        return Response({"detail": "minutes must be positive."}, status=400)

    # Extend from now when the planned end has already slipped past, so a
    # teacher who notices at 11:05 that an 11:00 class is overrunning gets the
    # full extra time they asked for instead of five minutes less.
    base = max(session.extended_until or session.end_time, timezone.now())
    new_end = base + timedelta(minutes=minutes)

    if new_end > session.max_extension_time:
        hours = int(LiveSession.MAX_EXTENSION.total_seconds() // 3600)
        return Response(
            {"detail": f"A class cannot run more than {hours} hours past its "
                       f"scheduled end time."},
            status=400,
        )

    session.extended_until = new_end
    session.save(update_fields=["extended_until"])
    broadcast_session_update(session)
    return Response({
        "detail": f"Class extended by {minutes} minutes.",
        "extended_until": new_end,
        "hard_end_time": session.hard_end_time,
    })


# =========================
# IN-CLASS MODERATION
# =========================

def _moderation_identities(session, target_id):
    """Every LiveKit identity ``target_id`` could be connected to this room under.

    Both moderation calls below used to address ``build_identity(target_id,
    session.id)`` with no profile argument, which produces the *teacher*-shaped
    ``"<uid>_x_<sid>"`` (``NO_PROFILE`` is the "not a learner" placeholder —
    see services/token.py). A student's token is minted with their learner
    profile, so they are in the room as ``"<uid>_<profile>_<sid>"`` and LiveKit
    was being asked to mute or disconnect an identity that does not exist. The
    call "succeeded" against nobody and the disruptive student stayed
    connected, camera and mic and all, for the rest of the class.

    A moderation request only carries a user id — the teacher clicked a name on
    the roster, and the roster is per account — so the profile has to be
    recovered here. Three sources, in confidence order:

      1. Open attendance intervals. Written from the participant_joined
         webhook, which sees the real identity, so this IS who is in the room.
      2. Every learner profile on the account, in case the webhook has not
         landed yet (a student removed seconds after joining).
      3. The no-profile shape, which is genuinely correct for teachers and for
         tokens minted before identities carried a profile.

    Order matters only cosmetically; the callers try all of them and treat one
    success as success.
    """
    from accounts.models import LearnerProfile
    from livestream.models import LiveSessionAttendanceInterval

    profile_ids = list(
        LiveSessionAttendanceInterval.objects
        .filter(session=session, user_id=target_id, left_at__isnull=True)
        .values_list("learner_profile_id", flat=True)
    )
    profile_ids += list(
        LearnerProfile.objects
        .filter(account_id=target_id)
        .values_list("id", flat=True)
    )
    profile_ids.append(None)   # the NO_PROFILE shape

    identities = []
    for pid in profile_ids:
        identity = build_identity(target_id, session.id, pid)
        if identity not in identities:
            identities.append(identity)
    return identities


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mute_live_participant(request, session_id):
    """Force-mute or unmute a participant. POST {user_id, muted?}"""
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if not _teacher_may_control(user, session):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    target_id = request.data.get("user_id")
    if not target_id:
        return Response({"detail": "user_id is required."}, status=400)
    if str(target_id) == str(user.id):
        return Response({"detail": "You cannot mute yourself here."}, status=400)

    muted = request.data.get("muted", True)
    from livestream.services.room_admin import mute_participant
    # Composite identity, and one attempt per shape the target could be in the
    # room under — see _moderation_identities. mute_participant raises for an
    # identity that is not in the room, so "not every attempt worked" is the
    # normal case; only "none of them worked" is a failure.
    muted_any = False
    for identity in _moderation_identities(session, target_id):
        try:
            mute_participant(session.room_name, identity, muted=bool(muted))
            muted_any = True
        except Exception:
            logger.debug("mute: %s is not in room %s", identity, session.room_name)
    if not muted_any:
        logger.warning("mute matched no participant (session=%s target=%s)",
                       session.pk, target_id)
        # Do NOT claim success: the teacher needs to know the student can
        # still be heard, so they can fall back to removing them.
        return Response({"detail": "Could not mute — the participant may have "
                                   "already left."}, status=502)

    return Response({"detail": "Muted." if muted else "Unmuted.",
                     "user_id": str(target_id), "muted": bool(muted)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_live_participant(request, session_id):
    """Eject a participant for the rest of this class. POST {user_id, reason?}

    Records the removal FIRST, then disconnects. That order matters: if the
    LiveKit call fails we still have the bar in place, so the student cannot
    quietly rejoin — whereas disconnecting first and failing to record would
    look successful and wear off the moment they refresh.
    """
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if not _teacher_may_control(user, session):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    target_id = request.data.get("user_id")
    if not target_id:
        return Response({"detail": "user_id is required."}, status=400)
    if str(target_id) == str(user.id):
        return Response({"detail": "You cannot remove yourself from your own "
                                   "class — use End instead."}, status=400)
    if str(target_id) == str(session.created_by_id):
        return Response({"detail": "The session's teacher cannot be removed."},
                        status=400)

    LiveSessionRemoval.objects.update_or_create(
        session=session, user_id=target_id,
        defaults={"removed_by": user,
                  "reason": (request.data.get("reason") or "")[:255],
                  "revoked_at": None},
    )
    # close_user, not close_intervals: we have no profile here, and
    # close_intervals would match only the NULL-profile rows — leaving the
    # removed learner's real interval open, i.e. 0 minutes on the roster.
    attendance_svc.close_user(session, _user_or_none(target_id),
                              when=timezone.now())

    from livestream.services.room_admin import remove_participant
    # One attempt per identity shape the target could be connected under —
    # see _moderation_identities for why a bare build_identity(target, session)
    # never matched a student at all.
    disconnected = False
    for identity in _moderation_identities(session, target_id):
        try:
            remove_participant(session.room_name, identity)
            disconnected = True
        except Exception:
            logger.debug("remove: %s is not in room %s", identity, session.room_name)
    if not disconnected:
        # The bar is already recorded, so they cannot rejoin — but they may
        # still be connected right now. Say so rather than implying silence.
        logger.warning("remove matched no participant (session=%s target=%s)",
                       session.pk, target_id)

    return Response({
        "detail": ("Removed from the class." if disconnected else
                   "Blocked from rejoining, but they may still be connected — "
                   "try again in a moment."),
        "user_id": str(target_id),
        "disconnected": disconnected,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def readmit_live_participant(request, session_id):
    """Undo a removal so the student can rejoin. POST {user_id}"""
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if not _teacher_may_control(user, session):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    target_id = request.data.get("user_id")
    if not target_id:
        return Response({"detail": "user_id is required."}, status=400)

    updated = LiveSessionRemoval.objects.filter(
        session=session, user_id=target_id, revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
    if not updated:
        return Response({"detail": "That participant is not removed."}, status=400)
    return Response({"detail": "Readmitted.", "user_id": str(target_id)})


def _user_or_none(user_id):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(id=user_id).first()


# =========================
# END SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def end_live_session(request, session_id):
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if not _teacher_may_control(user, session):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session already completed."}, status=400)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session is cancelled."}, status=400)

    session.status = LiveSession.STATUS_COMPLETED
    session.teacher_left_at = None
    # Unlike admin_stream_end / the room_finished webhook handler, this path
    # never stamped actual_ended_at — despite being the most common way a
    # session actually ends. Left NULL forever unless LiveKit's own
    # room_finished webhook happens to arrive later.
    if session.actual_ended_at is None:
        session.actual_ended_at = timezone.now()
    session.save(update_fields=["status", "teacher_left_at", "actual_ended_at"])

    # Close attendance locally instead of trusting LiveKit to round-trip a
    # participant_left/room_finished webhook back to us. This is the most
    # common way a class ends, and it was the only end path that did not do
    # this (admin_stream_end and the room_finished handler both do). If that
    # webhook is ever lost — retry exhaustion, a deploy window, a signature
    # rejection after a key rotation — the intervals stay left_at=NULL
    # forever, and nothing repairs them: the reconcile sweep only looks at
    # sessions that are still open (tasks.py `sample_live_viewers` filters on
    # non-terminal statuses), so a COMPLETED session is never revisited.
    # duration_seconds() reports 0 for an open interval, so a student who sat
    # through the whole class shows up on the teacher's roster as 0 minutes.
    # close_all_open is idempotent, so a later webhook doing it again is fine.
    attendance_svc.close_all_open(session, when=session.actual_ended_at)

    # Ending a session previously only flipped this DB flag — the LiveKit
    # room stayed open and every already-connected participant kept
    # publishing/consuming media regardless, bounded only by their token's
    # TTL (up to 2h). Close the room server-side so "End" actually ends it.
    close_room(session.room_name)

    broadcast_session_update(session)
    return Response({"detail": "Session ended.", "status": "COMPLETED"})


# =========================
# PAUSE / RESUME SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def pause_live_session(request, session_id):
    user = request.user
    session = get_object_or_404(LiveSession, id=session_id)

    require_teacher_context(request)

    if str(session.created_by_id) != str(user.id):
        return Response({"detail": "Only the session creator can pause."}, status=403)

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Cannot pause a cancelled session."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Cannot pause a completed session."}, status=400)

    if session.status == LiveSession.STATUS_PAUSED and not session.teacher_left_at:
        # Resume
        session.status = LiveSession.STATUS_LIVE
        session.teacher_left_at = None
        session.save(update_fields=["status", "teacher_left_at"])
        broadcast_session_update(session)
        return Response({"detail": "Session resumed.", "status": "LIVE"})

    # Pause — don't set teacher_left_at so the reconnect timer doesn't start
    session.status = LiveSession.STATUS_PAUSED
    session.teacher_left_at = None
    session.save(update_fields=["status", "teacher_left_at"])
    broadcast_session_update(session)
    return Response({"detail": "Session paused.", "status": "PAUSED"})


# =========================
# SESSION DETAIL
# =========================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def live_session_detail(request, session_id):
    session = get_object_or_404(LiveSession, id=session_id)
    user = request.user

    require_teacher_context(request)

    if not teaches_subject(user, session.subject):
        return Response({"detail": "Not assigned to this subject."}, status=403)

    from livestream.serializers import LiveSessionListSerializer
    session_data = LiveSessionListSerializer(session, context={"request": request}).data

    attendance = LiveSessionAttendance.objects.filter(session=session).select_related("user")
    attendance_data = [
        {
            "user_name": a.user.get_full_name() if hasattr(a.user, "get_full_name") else "",
            "user_email": a.user.email,
            "joined_at": a.joined_at.isoformat() if a.joined_at else None,
            "left_at": a.left_at.isoformat() if a.left_at else None,
        }
        for a in attendance
    ]

    return Response({"session": session_data, "attendance": attendance_data})


# =========================
# SESSION STATUS (lightweight poll, both sides)
# =========================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def live_session_status(request, session_id):
    """GET sessions/<id>/status/ — the classroom screen's WS-unavailable
    fallback poll (`LiveApi.sessionStatus`). Unlike live_session_detail
    (teacher-only, carries the attendance roster), this is reachable by BOTH
    sides: gated the same way join_live_session is — teacher-context +
    teaches_subject, or learner-context + an active enrollment in the
    session's course. No subscription check here: a lapsed subscription
    already 402s the actual join/token mint; it shouldn't also break the
    background status poll a student's classroom screen keeps running.
    """
    session = get_object_or_404(LiveSession, id=session_id)
    user = request.user

    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )

    if in_teacher_context:
        if not teaches_subject(user, session.subject):
            return Response({"detail": "Not assigned to this subject."}, status=403)
    else:
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(request)
        if learner is None:
            return Response({"detail": "Select a learner profile."}, status=403)
        if not _learner_may_access_session(learner, session):
            return Response({"detail": "Not enrolled in this course."}, status=403)

    return Response({
        "status": session.status,
        "computed_status": session.computed_status(),
    })


def _learner_may_access_session(learner, session):
    """Is this LEARNER PROFILE entitled to this live session?

    Two things every caller here used to get wrong:

    · ENROLMENT WAS ACCOUNT-KEYED (`Enrollment.filter(user=user)`), so on a
      one-email/many-children account any sibling's enrolment authorised any
      other child.
    · BATCH WAS NEVER CHECKED on the per-session paths, even though
      LiveSession.batch exists and the LIST view filters on it. That made the
      per-id endpoints a side door around the list's own rule — and in
      join_live_session it meant a morning-batch learner who knew a session id
      was issued a real LiveKit token for the evening class.

    NULL batch = course-wide. Mirrors the assignments/materials rule.
    """
    from enrollments.services import active_batch_id

    if not Enrollment.objects.filter(
        learner_profile=learner, course=session.course,
        status=Enrollment.STATUS_ACTIVE,
    ).exists():
        return False
    if session.batch_id is None:
        return True
    return session.batch_id == active_batch_id(
        learner_profile=learner, course_id=session.course_id,
    )


def _require_session_participant(request, session):
    """Same access gate as live_session_status: teacher-context + assigned to
    the subject, or learner-context + an active enrollment in the course.
    Raises PermissionDenied (→ 403) rather than returning a Response, so
    callers can use it as a one-line guard.
    """
    user = request.user
    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )

    if in_teacher_context:
        if not teaches_subject(user, session.subject):
            raise PermissionDenied("Not assigned to this subject.")
    else:
        from accounts.auth_flow import get_active_profile

        learner = get_active_profile(request)
        if learner is None:
            raise PermissionDenied("Select a learner profile.")
        if not _learner_may_access_session(learner, session):
            raise PermissionDenied("Not enrolled in this course.")


# =========================
# SESSION REVIEW (post-class rating)
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_session_review(request, session_id):
    """POST sessions/<id>/review/ — 1-5 star rating + optional description,
    submitted from the end-call review modal shown whenever a participant
    leaves the call. Not gated on the session being COMPLETED: a student
    leaving a still-live class (teacher continues for others) is a normal
    path here, and their review reflects their own experience up to that
    point. Resubmitting (e.g. modal shown again) overwrites the prior
    review rather than erroring, via update_or_create.
    """
    session = get_object_or_404(LiveSession, id=session_id)
    _require_session_participant(request, session)

    serializer = SessionReviewSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    review, _ = SessionReview.objects.update_or_create(
        session=session,
        user=request.user,
        defaults=serializer.validated_data,
    )

    return Response(SessionReviewSerializer(review).data, status=status.HTTP_201_CREATED)


# =========================
# SESSION NOTES (private per-user notes)
# =========================

@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def session_notes(request, session_id):
    """GET/PATCH sessions/<id>/notes/ — the requesting user's own private
    notes for this session (never another participant's). PATCH upserts via
    update_or_create so the in-call Notes panel can autosave without a
    separate create-vs-update branch.
    """
    session = get_object_or_404(LiveSession, id=session_id)
    _require_session_participant(request, session)

    if request.method == "GET":
        note = SessionNote.objects.filter(session=session, user=request.user).first()
        return Response(SessionNoteSerializer(note).data if note else {"content": "", "updated_at": None})

    serializer = SessionNoteSerializer(data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)

    note, _ = SessionNote.objects.update_or_create(
        session=session,
        user=request.user,
        defaults=serializer.validated_data,
    )

    return Response(SessionNoteSerializer(note).data)


# =========================
# LIVEKIT WEBHOOK
# =========================

def _event_room_name(event):
    room = getattr(event, "room", None)
    return getattr(room, "name", "") if room else ""


def _event_dedupe_id(event):
    """Stable idempotency key for a LiveKit webhook event.

    Prefer the event's own id; fall back to a composite so redeliveries of a
    payload without an id still collapse to one row.
    """
    eid = getattr(event, "id", None)
    if eid:
        return str(eid)
    parts = [
        getattr(event, "event", ""),
        _event_room_name(event),
        str(getattr(getattr(event, "participant", None), "identity", "")),
        str(getattr(event, "created_at", "")),
    ]
    return "|".join(parts)


@csrf_exempt
def livekit_webhook(request):
    """Signature-verified LiveKit webhook sink.

    Hardened for durability:
      • Signature failure → 400 (LiveKit will retry; that's correct).
      • Every accepted event is persisted to LiveKitWebhookEvent BEFORE dispatch.
      • Idempotent: a duplicate event_id is acknowledged (200) without re-running
        the handler, so LiveKit retries never double-count attendance.
      • A handler that throws is logged onto the event row and STILL returns 200
        — a single poison event must not wedge the whole webhook with retries.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    receiver = WebhookReceiver(
        TokenVerifier(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    )

    # Signature verification — the ONLY case where we ask LiveKit to retry.
    try:
        event = receiver.receive(
            request.body.decode("utf-8"),
            request.headers.get("Authorization"),
        )
    except Exception:
        logger.exception("LiveKit webhook signature/parse failure")
        return HttpResponse(status=400)

    event_type = getattr(event, "event", "") or ""
    room_name = _event_room_name(event)
    dedupe_id = _event_dedupe_id(event)
    logger.info("LiveKit event: %s room=%s", event_type, room_name)

    session = LiveSession.objects.filter(room_name=room_name).first() if room_name else None

    # Durable, idempotent log. get_or_create on the unique event_id collapses
    # redeliveries; if it already existed and was processed, we're done.
    try:
        payload = {}
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except Exception:
            payload = {}
        log_row, created = LiveKitWebhookEvent.objects.get_or_create(
            event_id=dedupe_id,
            defaults={
                "event_type": event_type,
                "room_name": room_name,
                "session": session,
                "payload": payload,
            },
        )
        if not created and log_row.processed:
            return HttpResponse(status=200)  # already handled — idempotent ack
    except Exception:
        # Logging must never drop the event; fall through and still try to handle.
        logger.exception("LiveKit webhook: failed to persist event log")
        log_row = None

    handlers = {
        "participant_joined": _handle_participant_join,
        "participant_left": _handle_participant_left,
        "room_started": _handle_room_started,
        "room_finished": _handle_room_finished,
    }
    handler = handlers.get(event_type)

    if handler:
        try:
            handler(event)
            if log_row:
                log_row.processed = True
                log_row.save(update_fields=["processed"])
        except Exception as exc:
            logger.exception("LiveKit webhook handler error: %s", event_type)
            if log_row:
                log_row.error = str(exc)[:2000]
                log_row.save(update_fields=["error"])
            # Still 200: retrying a poison event won't help; the row is our trail.
    else:
        if log_row:
            log_row.processed = True
            log_row.save(update_fields=["processed"])

    return HttpResponse(status=200)


@transaction.atomic
def _handle_participant_join(event):
    room_name = _event_room_name(event)
    # See _handle_participant_left: both handlers must take the same row lock
    # or the lock is worthless.
    session = LiveSession.objects.select_for_update().filter(
        room_name=room_name).first()
    if not session:
        _handle_group_session_join(room_name, event)
        return

    user_id = parse_identity(event.participant.identity)
    profile_id = parse_profile_id(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    now = timezone.now()

    # Append-only attendance interval (rejoins no longer overwrite), scoped to
    # the learner profile so two siblings on one account are two attendees.
    attendance_svc.open_interval(session, user, when=now,
                                 learner_profile=profile_id)

    session.last_activity_at = now
    update_fields = ["last_activity_at"]

    # First participant activity stamps the actual go-live time.
    if session.actual_started_at is None:
        session.actual_started_at = now
        update_fields.append("actual_started_at")

    if str(session.created_by_id) == user_id:
        session.teacher_left_at = None
        session.status = LiveSession.STATUS_LIVE
        update_fields += ["teacher_left_at", "status"]

    session.save(update_fields=update_fields)
    broadcast_session_update(session)

    # Notify enrolled students when teacher goes live.
    #
    # Gated to the window in which students can ACTUALLY join. The teacher
    # branch of join_live_session has no time check at all, so a teacher
    # opening the room the night before — or just early, to check their mic —
    # pushed "🔴 ... is now LIVE!" to every enrolled student in the batch,
    # while the student join gate refuses anyone until start_time − 15min.
    # Students tapped the notification and were told "Too early", which is
    # the kind of thing that teaches a whole cohort to ignore the bell.
    #
    # The 15 minutes deliberately mirrors that gate's own constant: if a
    # student can join, telling them is useful; if they cannot, it is noise.
    students_can_join_yet = now >= session.start_time - timedelta(minutes=15)

    if str(session.created_by_id) == user_id and students_can_join_yet:
        from livestream.services.notifications import push_ws_notification
        # Tell the people who can actually JOIN. Filtering on course alone
        # notified every batch, so the evening cohort got a "class is LIVE"
        # push for the morning class — which the join gate then 403s. Same
        # bug that was fixed for study-material uploads; _enrollments_for
        # applies the exact visibility rule the reader applies, and is the
        # only correct basis for a notification.
        from activity.signals import _enrollments_for
        students = _enrollments_for(session.course, session.batch_id).select_related("user")
        from notifications.services import notify as _notify
        title = f"🔴 {session.title} is now LIVE!"
        for enrollment in students:
            # Durable row first, so a student whose socket is closed still
            # finds out the class started. This lifecycle used to be
            # WS-ONLY: the frame was fire-and-forget and simply vanished for
            # anyone not currently connected, while the Skill Dev equivalent
            # wrote a full durable record. push_ws=False because the frame
            # below is the one the bell's click handler can actually route
            # (it carries object_id); notify()'s generic frame would arrive
            # as a SECOND, differently-id'd bell item for the same event.
            _notify(
                recipient=enrollment.user,
                actor=session.created_by,
                verb="livestream.started",
                title=title,
                # Per-PROFILE, not per-account: without these two a sibling's
                # bell shows the other child's class going live.
                audience_identity=(f"L:{enrollment.learner_profile_id}"
                                   if enrollment.learner_profile_id else ""),
                learner_profile=enrollment.learner_profile,
                link_url=f"/live/{session.id}",
                payload={"session_id": str(session.id),
                         "course_id": str(session.course_id)},
                push_ws=False,
            )
            push_ws_notification(enrollment.user.id, {
                "type": "live_session",
                "title": title,
                "session_id": str(session.id),
                # The bell's click handler resolves the join link off
                # `object_id` (matching the persisted Activity rows other
                # SESSION notifications carry) — without it, this transient
                # push has no id the frontend recognizes and falls back to
                # the plain session list.
                "id": str(session.id),
                "object_id": str(session.id),
                "start_time": session.start_time.isoformat(),
                # Track-scoped bells drop the other track's frames; without
                # this the row reads as cross-track and shows in both.
                "track": "academy",
            })


@transaction.atomic
def _handle_participant_left(event):
    room_name = _event_room_name(event)
    # Locked: participant_joined and participant_left arrive concurrently and
    # both mutate the session row. Without this, a read here can be flushed
    # over a commit that landed in between. @transaction.atomic alone gives
    # no such protection.
    session = LiveSession.objects.select_for_update().filter(
        room_name=room_name).first()
    if not session:
        _handle_group_session_left(room_name, event)
        return

    user_id = parse_identity(event.participant.identity)
    profile_id = parse_profile_id(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    now = timezone.now()

    # Close the open interval(s) and refresh the rollup (first/last/total).
    attendance_svc.close_intervals(session, user, when=now,
                                   learner_profile=profile_id)

    session.last_activity_at = now
    update_fields = ["last_activity_at"]

    if str(session.created_by_id) == user_id:
        # ALWAYS start the abandonment timer, even from PAUSED. This used to
        # skip it entirely for a manually-paused session, on the reasoning
        # that pause is deliberate — but that left a teacher who hit Pause and
        # then closed their laptop with a session that had no timer at all:
        # not LIVE, not ending, and still handing out valid join tokens to
        # students walking into an empty room until end_time.
        #
        # The status is still not overridden, so a teacher who is present and
        # paused stays PAUSED; computed_status() only walks the reconnect
        # ladder once teacher_left_at is set, which now means "the teacher is
        # actually gone" rather than "the teacher is gone AND wasn't paused".
        session.teacher_left_at = now
        update_fields.append("teacher_left_at")
        if session.status != LiveSession.STATUS_PAUSED:
            session.status = LiveSession.STATUS_RECONNECTING
            update_fields.append("status")

    # Write ONLY the fields this event actually changed. This used to save
    # teacher_left_at and status unconditionally, for every participant — so
    # a STUDENT leaving flushed back whatever those columns held when the row
    # was read at the top of this function. Interleaved with the teacher's
    # rejoin (which clears teacher_left_at and sets LIVE), the student's
    # handler would resurrect the stale "teacher is gone" timer, and
    # sync_open_session_statuses would then walk it to RECONNECTING, PAUSED
    # and finally COMPLETED 60 minutes later — force-ending a class that was
    # still running, in front of everyone, with no way for the teacher to
    # undo it. The select_for_update above closes the race; this closes the
    # blast radius if anything ever slips past it.
    session.save(update_fields=update_fields)
    broadcast_session_update(session)


def _handle_group_session_join(room_name, event):
    """No LiveSession matched this room — try GroupSession. Group session
    LiveKit identities are composite `"{user.id}_{session.id}"`, not a bare
    user id, so the user id has to be parsed out first."""
    from sessions_app.services import group_attendance as group_attendance_svc

    session = group_attendance_svc.resolve_group_session(room_name)
    if not session:
        _handle_private_session_join(room_name, event)
        return

    user_id = group_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    group_attendance_svc.open_interval(session, user, when=timezone.now())


def _handle_group_session_left(room_name, event):
    """No LiveSession matched this room — try GroupSession (see
    `_handle_group_session_join` for the identity-parsing note)."""
    from sessions_app.services import group_attendance as group_attendance_svc

    session = group_attendance_svc.resolve_group_session(room_name)
    if not session:
        _handle_private_session_left(room_name, event)
        return

    user_id = group_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    group_attendance_svc.close_intervals(session, user, when=timezone.now())


def _handle_private_session_join(room_name, event):
    """No LiveSession/GroupSession matched — try PrivateSession (1-on-1
    Academy sessions), same composite identity scheme as GroupSession."""
    from sessions_app.services import private_attendance as private_attendance_svc

    session = private_attendance_svc.resolve_private_session(room_name)
    if not session:
        _handle_skill_session_join(room_name, event)
        return

    user_id = private_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    private_attendance_svc.open_interval(session, user, when=timezone.now())


def _handle_private_session_left(room_name, event):
    """No LiveSession/GroupSession matched — try PrivateSession (see
    `_handle_private_session_join`)."""
    from sessions_app.services import private_attendance as private_attendance_svc

    session = private_attendance_svc.resolve_private_session(room_name)
    if not session:
        _handle_skill_session_left(room_name, event)
        return

    user_id = private_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    private_attendance_svc.close_intervals(session, user, when=timezone.now())


def _handle_skill_session_join(room_name, event):
    """No LiveSession/GroupSession/PrivateSession matched — try SkillSession
    (SkillDev 1-on-1 tutor sessions). Identity scheme is
    "expert-{id}"/"learner-{id}", not the composite scheme the other three
    features share — see skills/attendance.py."""
    from skills import attendance as skill_attendance_svc

    session = skill_attendance_svc.resolve_skill_session(room_name)
    if not session:
        return

    user_id = skill_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    skill_attendance_svc.open_interval(session, user, when=timezone.now())


def _handle_skill_session_left(room_name, event):
    """No LiveSession/GroupSession/PrivateSession matched — try SkillSession
    (see `_handle_skill_session_join`)."""
    from skills import attendance as skill_attendance_svc

    session = skill_attendance_svc.resolve_skill_session(room_name)
    if not session:
        return

    user_id = skill_attendance_svc.parse_user_id(str(event.participant.identity))
    if not user_id:
        return
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    skill_attendance_svc.close_intervals(session, user, when=timezone.now())


def _handle_room_started(event):
    # Defense-in-depth: this LiveKit event carries no participant identity
    # (unlike participant_joined below), so there is nothing here to gate a
    # creator check on — flipping status=LIVE unconditionally would mean
    # ANY participant's connection (a student arriving first, in a room a
    # teacher hasn't joined yet) marks the session live, bypassing the
    # created_by_id check _handle_participant_join correctly applies.
    # _handle_participant_join is the sole place that sets status=LIVE, so
    # this just records when the underlying room technically came into
    # existence.
    now = timezone.now()
    for session in LiveSession.objects.filter(room_name=_event_room_name(event)):
        if session.actual_started_at is None:
            session.actual_started_at = now
            session.save(update_fields=["actual_started_at"])
        broadcast_session_update(session)


def _handle_room_finished(event):
    now = timezone.now()
    for session in LiveSession.objects.filter(room_name=_event_room_name(event)):
        # Reconcile attendance: close every dangling interval so a missed
        # participant_left never orphans left_at forever.
        attendance_svc.close_all_open(session, when=now)

        # An EMPTY room is not a finished class.
        #
        # This used to complete the session on any room_finished, guarded only
        # by `!= CANCELLED`. But nothing in this codebase creates rooms
        # explicitly (no CreateRoom call, no empty_timeout anywhere), so
        # LiveKit auto-creates them and applies its own empty-room timeout —
        # a few minutes. A teacher who joined at 09:50 to check their mic for
        # a 10:00 class, then left, had the room go empty and this webhook
        # permanently mark the class COMPLETED. At 10:00 the teacher's own
        # join was rejected with "Session has ended." (that check runs before
        # the teacher branch), and nothing can undo it: reschedule and cancel
        # both refuse on a terminal status, and there is no reopen endpoint.
        # It also silently overrode the 60-minute reconnect grace the status
        # ladder exists to provide.
        #
        # So: always reconcile attendance above — a closed room really does
        # mean nobody is connected — but only treat the room closing as the
        # END of the class when the clock agrees. Genuine endings are already
        # covered without this: end_live_session sets COMPLETED itself before
        # closing the room, and an abandoned class still resolves through
        # teacher_left_at → RECONNECTING → PAUSED → COMPLETED.
        if session.status == LiveSession.STATUS_CANCELLED:
            continue
        if now < session.hard_end_time:
            # Mid-class (or pre-class) empty room. Leave the status ladder
            # alone so the teacher can still start or resume.
            continue

        session.status = LiveSession.STATUS_COMPLETED
        session.teacher_left_at = None
        if session.actual_ended_at is None:
            session.actual_ended_at = now
        session.save(update_fields=["status", "teacher_left_at", "actual_ended_at"])
        broadcast_session_update(session)
