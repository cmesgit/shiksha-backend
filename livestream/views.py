import json
from django.conf import settings
from .serializers import (
    LiveSessionCreateSerializer,
    LiveSessionUpdateSerializer,
    LiveSessionListSerializer,
    SessionReviewSerializer,
    SessionNoteSerializer,
)
from .services.token import generate_livekit_token
from .services.room_admin import close_room
from .services import attendance as attendance_svc
from .models import (
    LiveSession,
    LiveSessionAttendance,
    LiveKitWebhookEvent,
    SessionReview,
    SessionNote,
)
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

        active_enrollments = Enrollment.objects.filter(
            user=user,
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
            .select_related("course", "subject", "created_by")
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
                .select_related("course", "subject", "created_by")
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
            .select_related("course", "subject", "created_by")
            .order_by("start_time")
        )


# =========================
# JOIN SESSION
# =========================

from django_ratelimit.decorators import ratelimit
from django.utils.decorators import method_decorator

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def join_live_session(request, session_id):
    user = request.user
    # select_related: the response below reads course/subject/batch names.
    session = get_object_or_404(
        LiveSession.objects.select_related("course", "subject", "batch"),
        id=session_id,
    )
    now = timezone.now()

    if session.status == LiveSession.STATUS_CANCELLED:
        return Response({"detail": "Session was cancelled."}, status=400)

    if session.status == LiveSession.STATUS_COMPLETED:
        return Response({"detail": "Session has ended."}, status=400)

    if now >= session.end_time:
        session.status = LiveSession.STATUS_COMPLETED
        session.save(update_fields=["status"])
        return Response({"detail": "Session has ended."}, status=400)

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

    # ── Teacher ──
    if in_teacher_context:
        if not teaches_subject(user, session.subject):
            return Response({"detail": "Not assigned"}, status=403)

        is_creator = str(session.created_by_id) == str(user.id)
        is_teacher = is_creator

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
        if learner is None:
            return Response(
                {"detail": "Select a learner profile to join.", "lock_reason": "no_learner_profile"},
                status=403,
            )

        if not has_active_subscription(user=user, course=session.course, learner_profile=learner):
            return Response(lock_payload(user=user, course=session.course, learner_profile=learner), status=402)

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


# =========================
# END SESSION
# =========================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def end_live_session(request, session_id):
    user = request.user
    require_teacher_context(request)
    session = get_object_or_404(LiveSession, id=session_id)

    if str(session.created_by_id) != str(user.id):
        return Response({"detail": "Only the session creator can end it."}, status=403)

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
        if not Enrollment.objects.filter(
            user=user, course=session.course, status=Enrollment.STATUS_ACTIVE
        ).exists():
            return Response({"detail": "Not enrolled in this course."}, status=403)

    return Response({
        "status": session.status,
        "computed_status": session.computed_status(),
    })


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
        if not Enrollment.objects.filter(
            user=user, course=session.course, status=Enrollment.STATUS_ACTIVE
        ).exists():
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
    session = LiveSession.objects.filter(room_name=room_name).first()
    if not session:
        _handle_group_session_join(room_name, event)
        return

    user_id = str(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    now = timezone.now()

    # Append-only attendance interval (rejoins no longer overwrite).
    attendance_svc.open_interval(session, user, when=now)

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

    # Notify enrolled students when teacher goes live
    if str(session.created_by_id) == user_id:
        from livestream.services.notifications import push_ws_notification
        students = Enrollment.objects.filter(
            course=session.course,
            status=Enrollment.STATUS_ACTIVE
        ).select_related("user")
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
    session = LiveSession.objects.filter(room_name=room_name).first()
    if not session:
        _handle_group_session_left(room_name, event)
        return

    user_id = str(event.participant.identity)
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user:
        return

    now = timezone.now()

    # Close the open interval(s) and refresh the rollup (first/last/total).
    attendance_svc.close_intervals(session, user, when=now)

    session.last_activity_at = now

    if str(session.created_by_id) == user_id:
        # Only set reconnecting if session was LIVE — don't override manual PAUSED
        if session.status != LiveSession.STATUS_PAUSED:
            session.teacher_left_at = now
            session.status = LiveSession.STATUS_RECONNECTING
        # If manually paused, teacher just left — keep PAUSED, no timer

    session.save(update_fields=["teacher_left_at",
                 "status", "last_activity_at"])
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
        # Complete the session regardless of pause state
        if session.status != LiveSession.STATUS_CANCELLED:
            session.status = LiveSession.STATUS_COMPLETED
            session.teacher_left_at = None
            session.actual_ended_at = now
            session.save(update_fields=["status", "teacher_left_at", "actual_ended_at"])
            broadcast_session_update(session)
