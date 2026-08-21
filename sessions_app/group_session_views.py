"""
Group Session API endpoints.

All endpoints for the new Group Session feature live here so the existing
``views.py`` is untouched.  The notification-bell pattern mirrors
``_push_session_bell`` from ``views.py``; duplicated deliberately so
changes to either feature's notification copy don't cross-contaminate.
"""

from datetime import datetime, timedelta
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    GroupSession, GroupSessionInvite, GroupSessionChatMessage, GroupSessionNote,
    GroupSessionGuestSession, GroupSessionJoinRequest, GroupSessionAttendance,
    GroupSessionParticipant, GroupSessionReview, RemoteControlGrant, SessionFile,
)
from courses.board_display import board_name_for
from enrollments.models import Enrollment
from global_settings.models import GlobalSettings
from . import live_rules
from .permissions import IsStudent
from .serializers import get_user_name
from accounts.permissions import IsAdmin
from .services.group_session_token import generate_group_session_token
from .group_session_serializers import (
    GroupSessionNoteSerializer,
    GroupSessionCreateSerializer,
    GroupSessionDetailSerializer,
    GroupSessionInviteMoreSerializer,
    GroupSessionListSerializer,
    GroupSessionReviewSerializer,
    GroupSessionUpdateSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _course_label(course):
    """Human label for a course: "Class 11 — Science · CBSE".

    The ONE builder for both the create-modal picker (`my_course_subjects`) and
    the label denormalised onto `GroupSession.course_title` at create time, so
    the two can't drift. The board suffix matters because titles were
    normalised to drop it — two courses can now be titled "Class 9" and differ
    only by board. Requires select_related("course__stream", "course__board").
    """
    if course is None:
        return ""
    label = course.title
    if course.stream:
        label = f"{label} — {course.stream.name.title()}"
    board = board_name_for(course)
    if board:
        label = f"{label} · {board}"
    return label


def _gs_qs():
    """Base queryset with everything needed by the list serializer."""
    return (
        GroupSession.objects.select_related(
            "host",
            "invited_teacher",
            "subject", "subject__course__board",
        )
        .prefetch_related(
            Prefetch(
                "invites",
                queryset=GroupSessionInvite.objects.select_related("user"),
            )
        )
    )


def _can_view(session, user):
    """A session is visible to host, invited teacher, or any invitee.

    Instant meetings (session_type='instant') are visible to any
    authenticated user — possession of the short_code / share link is
    the gate, mirroring Google Meet. This lets students, teachers, and
    admins who paste the URL load the session detail and walk into
    the live room without being pre-invited.
    """
    if session.host_id == user.id:
        return True
    if session.invited_teacher_id and session.invited_teacher_id == user.id:
        return True
    if getattr(session, "session_type", "") == "instant":
        return True
    return session.invites.filter(user=user).exists()


# ---------------------------------------------------------------------------
# Capacity caps
# ---------------------------------------------------------------------------
# Hard ceiling on concurrent participants in an instant room. Counted from
# ``active_connections`` (incremented on WebSocket connect in consumers.py).
# The host is exempt so the room creator can always rejoin their own room.
# Scheduled group sessions are capped via ``GroupSession.max_invitees``
# (also 50 by default) at invite-add time, so no second cap is needed there.
INSTANT_MAX_PARTICIPANTS = 50


def _scheduled_aware_dt(session):
    """Return the tz-aware scheduled start datetime for a group session.

    The model stores scheduled_date (Date) + scheduled_time (Time); we
    combine them and interpret the result in the project's default
    timezone (Asia/Kolkata per settings_base.py).
    """
    from datetime import datetime
    return timezone.make_aware(
        datetime.combine(session.scheduled_date, session.scheduled_time)
    )


def _response_window_open(session):
    """
    A pending invite can only be accepted / declined / re-invited while
    the group is still in its *response window*:
        session.status == 'scheduled'  AND  scheduled_at >= now
    Once the scheduled time has passed, the UI must show the card as
    "Not attended" and both parties can no longer change their state.
    """
    if session.status != "scheduled":
        return False
    return _scheduled_aware_dt(session) > timezone.now()


def _before_room_started(session):
    """True if the session is still in the pre-launch phase.

    Used to gate:
      * host cancelling the group
      * accepted invitees un-accepting their response
    Once the first participant joins and status flips to 'live', neither
    action is allowed — the room must be ended instead.
    """
    if session.status != "scheduled":
        return False
    return session.room_started_at is None


# Deep links per side. Group sessions DO have a detail route
# (/sessions/group/<id>), which notifications/tasks.py already uses for the
# reminder sweep — reuse the exact same shape so a reminder and a lifecycle
# event about the same session land on the same page.
_GROUP_LINK = "/sessions/group/{id}"


def _emit_group_notification(user, title, session, verb, actor, is_teacher_role):
    """Durable Notification for a group-session lifecycle event.

    Additive — the Activity row and WS frame are unchanged and still drive
    the live bell. push_ws=False because the caller already pushes a frame
    for this same event; two frames carry different ids and would render as
    two separate bell items.

    Only fires when the call site named a `verb`. Several transitions here
    (accept/decline/withdraw acknowledgements to the host) deliberately stay
    Activity-only: they have no row in notifications/policy.py, so notify()
    would treat them as an unknown verb and route channels by fallback. Add
    the policy row first if one of them ever needs to survive being offline.
    """
    if not verb:
        return
    try:
        from notifications.services import notify

        identity = ""
        learner_profile = None
        if is_teacher_role:
            tp_id = getattr(getattr(user, "teacher_profile", None), "id", None)
            if tp_id:
                identity = f"T:{tp_id}"
        else:
            lp = user.default_learner_profile()
            if lp:
                identity = f"L:{lp.id}"
                learner_profile = lp

        notify(
            recipient=user,
            actor=actor,
            verb=verb,
            title=title,
            link_url=_GROUP_LINK.format(id=session.short_code or session.id),
            payload={"session_id": str(session.id), "kind": "group_session"},
            audience_identity=identity,
            learner_profile=learner_profile,
            push_ws=False,
        )
    except Exception:
        logger.exception("group-session durable notify failed (session=%s)",
                         session.id)


def _notify_user(user, title, session, verb=None, actor=None):
    """Create an Activity row + push a bell notification to ``user``.

    `verb` opts this event into a DURABLE notifications.Notification row as
    well (see _emit_group_notification). Left None by call sites whose
    transition has no policy row, which keeps their existing Activity-only
    behaviour exactly.

    Safe-by-design: never raises.
    """
    try:
        from activity.models import Activity
        from django.contrib.contenttypes.models import ContentType
        from livestream.services.notifications import push_ws_notification

        content_type = ContentType.objects.get_for_model(session)
        scheduled_dt = datetime.combine(
            session.scheduled_date, session.scheduled_time
        )

        # Make sure the saved due_date is timezone-aware. ``datetime.combine``
        # returns a naive datetime; saving naive datetimes when USE_TZ=True
        # emits warnings and (depending on Django version) can blow up
        # downstream comparisons. Force-aware in the project tz.
        if timezone.is_naive(scheduled_dt):
            scheduled_dt = timezone.make_aware(scheduled_dt)

        # ``user`` here is either the host (always a student in this
        # marketplace) or an invitee — invitees can be either role
        # (invite_role="teacher" for an invited co-teacher). Tag
        # audience/learner_profile from that, same fix as private sessions'
        # _push_session_bell — a NULL learner_profile on a LEARNER row is
        # visible to every profile on the account, reopening the sibling
        # leak fixed for quizzes/assignments.
        is_teacher_role = (
            (session.invited_teacher_id and session.invited_teacher_id == user.id)
            or session.invites.filter(user=user, invite_role="teacher").exists()
        )
        audience = Activity.AUDIENCE_TEACHER if is_teacher_role else Activity.AUDIENCE_LEARNER
        learner_profile = None if is_teacher_role else user.default_learner_profile()

        activity, created = Activity.objects.get_or_create(
            user=user,
            type=Activity.TYPE_SESSION,
            content_type=content_type,
            object_id=session.id,
            title=title,
            defaults={
                # Match the shape used by other notification producers
                # (assignments, quizzes, live sessions) so the dashboard
                # serializer never sees a NULL subject_id from one feature
                # and a UUID from another.
                "subject_id": session.subject_id,
                "subject_name": session.subject_name,
                "due_date": scheduled_dt,
                "audience": audience,
                "learner_profile": learner_profile,
            },
        )
        if created:
            # Inside the `created` guard: get_or_create is already this
            # layer's "is this a NEW real event" ledger, so a retried
            # transition can't spend a second email or SMS.
            _emit_group_notification(user, title, session, verb, actor,
                                     is_teacher_role)
            push_ws_notification(user.id, {
                "type": "SESSION",
                "title": title,
                "subject_name": session.subject_name,
                "id": str(session.id),
                "is_read": False,
                "created_at": activity.created_at.isoformat(),
                "is_group_session": True,
                # Lets the track-scoped bell drop this frame when the user
                # is viewing Skill Dev; without it the row reads as
                # cross-track and shows in BOTH bells.
                "track": "academy",
            })
    except Exception:
        logger.exception("Failed to push group-session notification")


def _broadcast(session):
    """Push list-shape session_update to host + all invited users."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    # Re-fetch with prefetches so counts are correct
    full = _gs_qs().get(pk=session.pk)
    data = GroupSessionListSerializer(full).data

    user_ids = {str(session.host_id)}
    if session.invited_teacher_id:
        user_ids.add(str(session.invited_teacher_id))
    for uid in session.invites.values_list("user_id", flat=True):
        user_ids.add(str(uid))

    for uid in user_ids:
        try:
            async_to_sync(channel_layer.group_send)(
                f"user_{uid}",
                {"type": "session_update", "data": data},
            )
        except Exception:
            pass


def _end_group_session_internal(session, reason="ended"):
    """Finalise a live session. Used by hard-duration task, idle cleanup, and cancel-live.

    Per product spec, group-session chat persists only while the room is live —
    on end, all chat messages for this session are dropped from the DB.
    Wrapped in atomic so we never end a session while leaving stale chat
    rows behind on a delete failure.
    """
    if session.status != "live":
        return False
    with transaction.atomic():
        session.status = "completed"
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated_at"])
        deleted, _ = GroupSessionChatMessage.objects.filter(session=session).delete()
        # Close out any still-open GroupSessionParticipant rows (best-effort
        # "who's in the room" tracking — see that model's docstring). This is
        # the one place we can reliably say "the room is over, everyone is
        # out", even though a mid-session tab-close isn't individually
        # detected today.
        GroupSessionParticipant.objects.filter(
            session=session, left_at__isnull=True
        ).update(left_at=session.ended_at)

    # End the CALL too, outside the transaction — a network round-trip must
    # not hold a DB lock open. Without this the hard-duration cutoff marked
    # the meeting completed and purged its chat while everyone carried on
    # talking, for up to the 60-minute token TTL.
    try:
        from livestream.services.room_admin import close_room
        if getattr(session, "room_name", None):
            close_room(session.room_name)
    except Exception:
        logger.warning("GroupSession %s: close_room failed", session.id,
                       exc_info=True)

    logger.info(
        "GroupSession %s ended (reason: %s) — purged %d chat msgs",
        session.id, reason, deleted,
    )
    return True


def _schedule_hard_duration_cutoff(session):
    """Queue a Celery task that force-ends the room at duration expiry."""
    try:
        from .group_session_tasks import hard_expire_group_session
        eta = session.room_started_at + timedelta(minutes=session.duration_minutes)
        hard_expire_group_session.apply_async(args=[str(session.id)], eta=eta)
    except Exception:
        logger.exception("Failed to schedule hard-duration cutoff for %s", session.id)


# ---------------------------------------------------------------------------
# Lookup endpoints (used by the "Create" modal)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsStudent])
def my_course_subjects(request):
    """
    List subjects for the course(s) the authenticated student is enrolled in.

    Returns grouped subjects per course so the UI can render a nicely-
    labelled dropdown when the student is enrolled in multiple courses.
    """
    from courses.models import Subject
    from enrollments.models import Enrollment

    enrollments = Enrollment.objects.filter(
        user=request.user, status=Enrollment.STATUS_ACTIVE
    ).select_related("course", "course__stream", "course__board")

    out = []
    for enr in enrollments:
        course = enr.course
        course_label = _course_label(course)
        subjects = Subject.objects.filter(course=course).order_by("order", "name")
        out.append({
            "course_id": str(course.id),
            "course_label": course_label,
            "subjects": [
                {"id": str(s.id), "name": s.name} for s in subjects
            ],
        })
    return Response(out)


# ---------------------------------------------------------------------------
# Create / invite-more
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def create_group_session(request):
    ser = GroupSessionCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data

    from courses.models import Subject, TeachingAssignment
    from enrollments.models import Enrollment

    # ── Validate subject + enrollment ────────────────────────────────
    try:
        subject = Subject.objects.select_related(
        "course", "course__stream", "course__board").get(
            pk=d["subject_id"]
        )
    except Subject.DoesNotExist:
        return Response({"error": "Invalid subject."}, status=400)

    if not Enrollment.objects.filter(
        user=request.user,
        course=subject.course,
        status=Enrollment.STATUS_ACTIVE,
    ).exists():
        return Response(
            {"error": "You are not enrolled in this subject's course."},
            status=403,
        )

    # ── Validate invited teacher (if any) teaches this subject ───────
    invited_teacher = None
    invited_teacher_id = d.get("invited_teacher_id")
    if invited_teacher_id:
        if not TeachingAssignment.objects.filter(
            subject=subject, teacher_id=invited_teacher_id, is_active=True,
        ).exists():
            return Response(
                {"error": "That teacher does not teach this subject."},
                status=400,
            )
        try:
            invited_teacher = User.objects.get(pk=invited_teacher_id)
        except User.DoesNotExist:
            return Response({"error": "Teacher not found."}, status=404)

    # ── Validate invitees are enrolled in the same course ────────────
    invited_user_ids = [str(uid) for uid in d["invited_user_ids"]]
    if str(request.user.id) in invited_user_ids:
        return Response(
            {"error": "Host cannot invite themselves."}, status=400
        )

    valid_invitee_ids = set(
        Enrollment.objects.filter(
            course=subject.course,
            status=Enrollment.STATUS_ACTIVE,
            user_id__in=invited_user_ids,
        ).values_list("user_id", flat=True)
    )
    valid_invitee_ids = {str(uid) for uid in valid_invitee_ids}

    bad = [uid for uid in invited_user_ids if uid not in valid_invitee_ids]
    if bad:
        return Response(
            {"error": "Some invitees are not enrolled in this course.",
             "invalid_user_ids": bad},
            status=400,
        )

    # ── Build the course label ───────────────────────────────────────
    course_label = _course_label(subject.course)

    # ── Create everything atomically ─────────────────────────────────
    with transaction.atomic():
        session = GroupSession.objects.create(
            host=request.user,
            invited_teacher=invited_teacher,
            subject=subject,
            subject_name=subject.name,
            course_title=course_label,
            topic=d.get("topic", ""),
            scheduled_date=d["scheduled_date"],
            scheduled_time=d["scheduled_time"],
            duration_minutes=d["duration_minutes"],
            status="scheduled",
        )

        invites = []
        for uid in valid_invitee_ids:
            invites.append(GroupSessionInvite(
                session=session, user_id=uid, invite_role="student",
            ))
        if invited_teacher:
            invites.append(GroupSessionInvite(
                session=session, user_id=invited_teacher.id, invite_role="teacher",
            ))
        GroupSessionInvite.objects.bulk_create(invites)

    # ── Notify each invitee ──────────────────────────────────────────
    host_name = get_user_name(request.user)
    for inv in GroupSessionInvite.objects.filter(session=session).select_related("user"):
        if inv.invite_role == "teacher":
            title = f"📚 {host_name} invited you to a {session.subject_name} group session"
        else:
            title = f"📚 {host_name} invited you to a {session.subject_name} group session"
        _notify_user(inv.user, title, session,
                     verb="group.invite", actor=request.user)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def invite_more(request, session_id):
    """Add more invitees after the fact (host only, while status=scheduled)."""
    try:
        session = GroupSession.objects.select_related(
            "subject", "subject__course__board"
        ).get(pk=session_id, host=request.user)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.status != "scheduled":
        return Response(
            {"error": "Can only invite more while the group is scheduled."},
            status=400,
        )

    ser = GroupSessionInviteMoreSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ids = [str(uid) for uid in ser.validated_data["invited_user_ids"]]

    from enrollments.models import Enrollment

    valid = set(
        Enrollment.objects.filter(
            course=session.subject.course,
            status=Enrollment.STATUS_ACTIVE,
            user_id__in=ids,
        ).values_list("user_id", flat=True)
    )
    valid = {str(uid) for uid in valid}

    existing = set(
        session.invites.values_list("user_id", flat=True)
    )
    existing = {str(uid) for uid in existing}

    current_total = session.invites.count()
    to_add_ids = [uid for uid in ids if uid in valid and uid not in existing]
    if current_total + len(to_add_ids) > session.max_invitees:
        return Response(
            {"error": f"Cannot exceed {session.max_invitees} invitees."},
            status=400,
        )

    if not to_add_ids:
        return Response({"error": "No new valid invitees."}, status=400)

    GroupSessionInvite.objects.bulk_create([
        GroupSessionInvite(session=session, user_id=uid, invite_role="student")
        for uid in to_add_ids
    ])

    host_name = get_user_name(request.user)
    for inv in session.invites.filter(user_id__in=to_add_ids).select_related("user"):
        _notify_user(
            inv.user,
            f"📚 {host_name} invited you to a {session.subject_name} group session",
            session,
            verb="group.invite", actor=request.user,
        )

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Invitee responses
# ---------------------------------------------------------------------------


def _get_invite_for_user(session_id, user):
    return (
        GroupSessionInvite.objects.select_related(
            "session", "session__host", "session__subject",
        )
        .filter(session_id=session_id, user=user)
        .first()
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def accept_invite(request, session_id):
    invite = _get_invite_for_user(session_id, request.user)
    if not invite:
        return Response({"error": "Invite not found."}, status=404)

    if invite.status == "accepted":
        return Response({"error": "Already accepted."}, status=400)

    session = invite.session
    if session.status not in ("scheduled", "live"):
        return Response(
            {"error": f"Group is {session.status}; cannot accept."},
            status=400,
        )

    # If the scheduled time has passed, the invite is stale — no one can
    # accept or decline any more. The card will be auto-moved to history
    # by the cleanup command 6 hours later.
    if session.status == "scheduled" and _scheduled_aware_dt(session) <= timezone.now():
        return Response(
            {"error": "This group session's start time has passed; you can no longer respond."},
            status=400,
        )

    invite.status = "accepted"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    # Notify the host (the user who initiated the group session request).
    # Use a slightly different copy when a TEACHER accepts, so the host
    # knows the room can already be opened on their authority.
    responder_label = "Teacher" if invite.invite_role == "teacher" else ""
    actor_name = get_user_name(request.user)
    if responder_label:
        title = (
            f"✅ {responder_label} {actor_name} accepted your "
            f"{session.subject_name} group session"
        )
    else:
        title = (
            f"✅ {actor_name} accepted your {session.subject_name} group session"
        )
    _notify_user(session.host, title, session)
    _broadcast(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decline_invite(request, session_id):
    invite = _get_invite_for_user(session_id, request.user)
    if not invite:
        return Response({"error": "Invite not found."}, status=404)

    if invite.decline_count >= 2:
        return Response({"error": "Already declined twice."}, status=400)

    session = invite.session
    # Block late decline once the scheduled time has passed — this mirrors
    # the accept_invite window so neither side can thrash a stale card.
    if session.status == "scheduled" and _scheduled_aware_dt(session) <= timezone.now():
        return Response(
            {"error": "This group session's start time has passed; you can no longer respond."},
            status=400,
        )

    invite.status = "declined"
    invite.decline_count = invite.decline_count + 1
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "decline_count", "responded_at"])

    session = invite.session
    responder_label = "Teacher" if invite.invite_role == "teacher" else ""
    actor_name = get_user_name(request.user)
    if responder_label:
        title = (
            f"↩ {responder_label} {actor_name} declined your "
            f"{session.subject_name} group session"
        )
    else:
        title = (
            f"↩ {actor_name} declined your {session.subject_name} group session"
        )
    _notify_user(session.host, title, session)
    _broadcast(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def reinvite(request, session_id):
    """Host re-invites a single user who previously declined (allowed once)."""
    try:
        session = GroupSession.objects.get(pk=session_id, host=request.user)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.status != "scheduled":
        return Response(
            {"error": "Can only re-invite while scheduled."}, status=400
        )

    # After the start time has passed, the card is read-only on both sides.
    if _scheduled_aware_dt(session) <= timezone.now():
        return Response(
            {"error": "This group session's start time has passed; you can no longer re-invite."},
            status=400,
        )

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"error": "user_id is required."}, status=400)

    invite = session.invites.filter(user_id=user_id).first()
    if not invite:
        return Response({"error": "Invite not found."}, status=404)
    if invite.status != "declined":
        return Response(
            {"error": "Can only re-invite after decline."}, status=400
        )
    if invite.decline_count >= 2:
        return Response(
            {"error": "Already declined twice; cannot re-invite."}, status=400
        )
    if invite.reinvited_at:
        return Response(
            {"error": "Already re-invited once."}, status=400
        )

    invite.status = "pending"
    invite.reinvited_at = timezone.now()
    invite.save(update_fields=["status", "reinvited_at"])

    host_name = get_user_name(request.user)
    _notify_user(
        invite.user,
        f"📚 {host_name} re-invited you to their {session.subject_name} group session",
        session,
        verb="group.invite", actor=request.user,
    )
    _broadcast(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Un-accept — invitee takes back an "accepted" response
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def unaccept_invite(request, session_id):
    """
    Let an accepted invitee flip their status back to 'pending' any time
    before the room has actually opened (room_started_at is null).

    This is distinct from ``decline_invite`` — it doesn't increment the
    decline counter, doesn't burn the single re-invite, and leaves the
    host with the option of expecting the user again should they re-accept.
    """
    invite = _get_invite_for_user(session_id, request.user)
    if not invite:
        return Response({"error": "Invite not found."}, status=404)

    if invite.status != "accepted":
        return Response(
            {"error": "You can only cancel an attendance you previously accepted."},
            status=400,
        )

    session = invite.session
    if not _before_room_started(session):
        return Response(
            {"error": "The room has already started; you can't cancel attendance now."},
            status=400,
        )
    if _scheduled_aware_dt(session) <= timezone.now():
        return Response(
            {"error": "This group session's start time has passed; you can no longer change your response."},
            status=400,
        )

    invite.status = "pending"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    # Let the host know someone just stepped back.
    _notify_user(
        session.host,
        f"↩ {get_user_name(request.user)} is no longer attending your {session.subject_name} group session",
        session,
    )
    _broadcast(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Cancel / listing / detail
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def cancel_group_session(request, session_id):
    try:
        session = GroupSession.objects.get(pk=session_id, host=request.user)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    # Only the pre-launch window allows cancellation. Once the room has
    # started (status='live') it must be ended normally instead.
    if not _before_room_started(session):
        if session.status == "live":
            msg = "The room has already started; you can't cancel it any more."
        else:
            msg = f"Cannot cancel a group that is {session.status}."
        return Response({"error": msg}, status=400)

    session.status = "cancelled"
    session.cancel_reason = request.data.get("reason", "")
    session.ended_at = timezone.now()
    session.save(update_fields=["status", "cancel_reason", "ended_at", "updated_at"])

    host_name = get_user_name(request.user)
    for inv in session.invites.select_related("user"):
        _notify_user(
            inv.user,
            f"❌ {host_name} cancelled the {session.subject_name} group session",
            session,
            verb="group.cancelled", actor=request.user,
        )
    _broadcast(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_group_sessions(request):
    """
    Tabs:
      ?tab=upcoming    → scheduled + live groups I host or am accepted into
                         (excluding past-time groups whose room never opened —
                          those land in History straight away, no waiting on
                          the 6h cleanup cron).
      ?tab=invites     → groups where I have a pending invite (response window
                         still open: scheduled status AND start time in the future).
      ?tab=history     → completed / cancelled / expired I was part of, PLUS
                         scheduled-but-orphan groups whose start time has passed
                         (the cleanup cron will flip these to ``expired`` later;
                         we surface them here immediately so the UI doesn't
                         mislead the user).
    """
    tab = request.query_params.get("tab", "upcoming")
    user = request.user

    base = _gs_qs()

    # Compute "past-time orphan" Q: a scheduled group whose start instant has
    # already elapsed but the room was never opened. Built from local date+time
    # since the model stores naive Date + Time fields (interpreted as project
    # default tz, Asia/Kolkata).
    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    now_t = now_local.time()
    past_orphan_q = (
        Q(status="scheduled") & Q(room_started_at__isnull=True) & (
            Q(scheduled_date__lt=today)
            | Q(scheduled_date=today, scheduled_time__lte=now_t)
        )
    )

    if tab == "invites":
        # Pending invitations are only actionable while the response window
        # is still open (scheduled + future start time).
        qs = base.filter(
            invites__user=user,
            invites__status="pending",
            status="scheduled",
        ).exclude(past_orphan_q)
    elif tab == "history":
        qs = base.filter(
            Q(host=user) | Q(invites__user=user) | Q(invited_teacher=user),
        ).filter(
            Q(status__in=["completed", "cancelled", "expired"])
            | past_orphan_q
        ).exclude(hidden_for=user)
    else:  # upcoming (default)
        qs = base.filter(
            Q(host=user)
            | Q(invites__user=user, invites__status="accepted")
            | Q(invited_teacher=user, invites__user=user, invites__status="accepted"),
            status__in=["scheduled", "live"],
        ).exclude(past_orphan_q)

    qs = qs.distinct().order_by("scheduled_date", "scheduled_time")
    items = list(qs)

    # Upcoming-only safety filter: drop live sessions whose hard-duration
    # has already elapsed but whose status hasn't yet been flipped to
    # 'completed' by the Celery cutoff task or the idle-cleanup cron. The
    # backend usually catches these via _schedule_hard_duration_cutoff or
    # the next /join/ attempt, but neither fires if the room sits idle
    # past its end time without anyone touching it. Without this we'd be
    # serving cards that the UI then disables / errors on.
    if tab == "upcoming":
        now = timezone.now()
        items = [
            s for s in items
            if not (
                s.status == "live"
                and s.room_started_at is not None
                and (now - s.room_started_at).total_seconds()
                    >= s.duration_minutes * 60
            )
        ]

    # Use the Detail serializer here so each card carries its full ``invites``
    # array. Card rendering only consumes the count fields (which are present
    # in both serializers), but the frontend opens the Detail view directly
    # from a card click without re-fetching, so it needs ``invites`` populated.
    # Without this, teacher-side Accept/Decline buttons never render
    # (myStatus is null because invitesList is empty).
    # Cost is zero: ``_gs_qs()`` already prefetches the invites + their users.
    return Response(GroupSessionDetailSerializer(items, many=True).data)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def group_session_detail(request, session_id):
    if request.method == "PATCH":
        return _update_group_session(request, session_id)

    try:
        session = _gs_qs().get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this group session."}, status=403
        )
    return Response(GroupSessionDetailSerializer(session).data)


def _update_group_session(request, session_id):
    """Edit a still-``scheduled`` group session (host only): topic, date,
    time, duration, plus additive invitee changes. Existing invites (accepted
    or pending) are never removed here — revoking an invite is a separate,
    more sensitive action this endpoint doesn't attempt; only ids in
    ``invited_user_ids`` that aren't already invited get added, exactly like
    ``invite_more``. ``invited_teacher_id`` from the same payload is ignored —
    there's no established "change the invited teacher" flow to reuse, and
    guessing at one risks silently dropping a session's original co-teacher.
    """
    try:
        session = GroupSession.objects.select_related(
            "subject", "subject__course__board"
        ).get(pk=session_id, host=request.user)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.status != "scheduled":
        return Response(
            {"error": f"Cannot edit a group session that is {session.status}."},
            status=400,
        )

    ser = GroupSessionUpdateSerializer(data=request.data, context={"session": session})
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    fields = []
    for key in ("scheduled_date", "scheduled_time", "duration_minutes", "topic"):
        if key in data:
            setattr(session, key, data[key])
            fields.append(key)
    if fields:
        fields.append("updated_at")
        session.save(update_fields=fields)

    raw_ids = request.data.get("invited_user_ids")
    if raw_ids:
        from enrollments.models import Enrollment

        ids = [str(uid) for uid in raw_ids]
        valid = {
            str(uid) for uid in Enrollment.objects.filter(
                course=session.subject.course,
                status=Enrollment.STATUS_ACTIVE,
                user_id__in=ids,
            ).values_list("user_id", flat=True)
        }
        existing = {str(uid) for uid in session.invites.values_list("user_id", flat=True)}
        room_left = session.max_invitees - session.invites.count()
        to_add_ids = [uid for uid in ids if uid in valid and uid not in existing][:max(room_left, 0)]

        if to_add_ids:
            GroupSessionInvite.objects.bulk_create([
                GroupSessionInvite(session=session, user_id=uid, invite_role="student")
                for uid in to_add_ids
            ])
            host_name = get_user_name(request.user)
            for inv in session.invites.filter(user_id__in=to_add_ids).select_related("user"):
                _notify_user(
                    inv.user,
                    f"📚 {host_name} invited you to a {session.subject_name} group session",
                    session,
                    verb="group.invite", actor=request.user,
                )

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Per-user "hide from history" — soft-delete scoped to the requesting user.
# The session row itself, the host's view, and other participants' views are
# untouched. This is exclusively a History-tab cleanup mechanism.
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hide_group_session_for_me(request, session_id):
    """Hide a single group session from MY History view.

    Caller must have had access to the session (host / invited teacher /
    invitee). Adding the same user to ``hidden_for`` twice is a no-op (M2M
    ``add()`` is idempotent), so retries are safe.
    """
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this group session."}, status=403
        )

    session.hidden_for.add(request.user)
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def clear_my_group_session_history(request):
    """Bulk-hide history entries for the requesting user.

    Body shapes (both supported):
      {"all": true}                  → hide every history-tab session for me
      {"session_ids": [<uuid>, ...]} → hide just the listed set

    Mirrors the History queryset built in ``my_group_sessions`` so we never
    hide a session the user couldn't already see in History. Returns the
    number of sessions actually affected (idempotent on already-hidden ones).
    """
    user = request.user
    body = request.data or {}

    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    now_t = now_local.time()
    past_orphan_q = (
        Q(status="scheduled") & Q(room_started_at__isnull=True) & (
            Q(scheduled_date__lt=today)
            | Q(scheduled_date=today, scheduled_time__lte=now_t)
        )
    )
    visible_to_me_q = (
        Q(host=user) | Q(invites__user=user) | Q(invited_teacher=user)
    )

    qs = GroupSession.objects.filter(visible_to_me_q).filter(
        Q(status__in=["completed", "cancelled", "expired"]) | past_orphan_q
    ).distinct()

    if body.get("all") is True:
        target_ids = list(qs.values_list("id", flat=True))
    else:
        raw_ids = body.get("session_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {"error": "Provide either {'all': true} or "
                          "{'session_ids': [...]}"},
                status=400,
            )
        # Intersect requested ids with what the user is actually allowed to
        # hide — silently drops ids that weren't theirs (or weren't in
        # history). The user gets a count back so they know if anything
        # was filtered.
        target_ids = list(qs.filter(id__in=raw_ids).values_list("id", flat=True))

    if not target_ids:
        return Response({"ok": True, "hidden_count": 0})

    # M2M's through-table; use the reverse side so we issue exactly one
    # INSERT for each (session, user) pair that doesn't already exist.
    user.hidden_group_sessions.add(*target_ids)
    return Response({"ok": True, "hidden_count": len(target_ids)})


# ---------------------------------------------------------------------------
# Join (LiveKit token) — opens the room on first join
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_group_session(request, session_id):
    """
    Returns a LiveKit token if the caller may join.

    Side-effects on first join:
      * flips status from scheduled → live
      * assigns room_name + room_started_at
      * schedules a Celery task at room_started_at + duration for the
        hard-duration cutoff.
    """
    # NOTE: do not use ``select_for_update()`` on this initial read. We're
    # in autocommit (no surrounding ``transaction.atomic()``) and Django
    # raises ``TransactionManagementError`` if SELECT ... FOR UPDATE is
    # issued in autocommit mode on Postgres — that exception is not a
    # DRF ``APIException`` so it escapes DRF and Django returns its raw
    # HTML 500 page (which is what the host saw when clicking START ROOM).
    # The row is locked INSIDE the atomic flip block below instead.
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    user = request.user

    # Paywall gate — only paid dashboard users may consume LiveKit minutes.
    # Currently a stub (see _is_paid_user) that defaults to True; wire it
    # to your subscription model when entitlements land.
    if not _is_paid_user(user):
        return Response(
            {"error": "Your account is not eligible to join meetings."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    # Auth check.
    #
    # Roles in a group session:
    #   * Host:  the student who created the group. Implicitly accepted —
    #            no invite row exists for them. Only the host may flip the
    #            status from scheduled → live (start the room).
    #   * Invited teacher / invited student: must explicitly accept their
    #            own invite before they may join. They cannot start the
    #            room; they wait until the host opens it.
    # Instant meetings (session_type='instant') skip the invite gate entirely:
    # anyone authenticated and paid who has the link can join. The host is
    # still the only one allowed to /end/ the room.
    is_instant = (session.session_type == "instant")
    is_host = (session.host_id == user.id)
    invite = session.invites.filter(user=user).first()
    is_accepted_invitee = bool(invite and invite.status == "accepted")
    is_invited_teacher = bool(
        session.invited_teacher_id and session.invited_teacher_id == user.id
        and is_accepted_invitee
    )

    if is_host:
        # Implicit accept; no further gate.
        pass
    elif is_instant:
        # Open join for instant meetings — auth + paywall above are the only
        # gates. (admit_mode='lobby' is enforced separately below, after this
        # branch, for any non-host caller regardless of session type.)
        pass
    elif invite is None:
        return Response(
            {"error": "You are not a participant in this group session."},
            status=403,
        )
    elif invite.status == "declined":
        return Response(
            {"error": "You declined this invite, so you can't join the room."},
            status=403,
        )
    elif invite.status != "accepted":
        return Response(
            {"error": "You must accept the invite before you can join the room."},
            status=403,
        )

    # Capacity cap for instant rooms.
    # ``active_connections`` is incremented in consumers.py on WS connect and
    # decremented on disconnect, so it reflects the current live headcount.
    # The host is exempt — the room creator can always rejoin their own room
    # even if it's "full". Locked (matching the nearby host-flip block below)
    # so a burst of concurrent /join/ calls can't all read the same
    # pre-increment count and all get admitted past the cap.
    if is_instant and not is_host:
        with transaction.atomic():
            locked_session = (
                GroupSession.objects.select_for_update().get(pk=session.pk)
            )
            if (locked_session.active_connections or 0) >= INSTANT_MAX_PARTICIPANTS:
                return Response(
                    {"error": f"This instant meeting is full "
                              f"({INSTANT_MAX_PARTICIPANTS} participants max). "
                              f"Please try again once someone leaves."},
                    status=http_status.HTTP_403_FORBIDDEN,
                )

    # "Knock to join" — a non-host caller must be admitted before they get a
    # token. Re-knocking after a prior denial resets that same row back to
    # pending (one row per session+user, not a history of attempts).
    if session.admit_mode == "lobby" and not is_host:
        join_req, _created = GroupSessionJoinRequest.objects.get_or_create(
            session=session, user=user
        )
        if join_req.status == "denied":
            join_req.status = "pending"
            join_req.resolved_at = None
            join_req.deny_message = ""
            join_req.save(update_fields=["status", "resolved_at", "deny_message"])
        if join_req.status != "admitted":
            return Response(
                {"status": "pending", "join_request_id": str(join_req.id)},
                status=http_status.HTTP_202_ACCEPTED,
            )

    # Early terminal states
    if session.status in ("cancelled", "completed", "expired"):
        return Response(
            {"error": f"Group is {session.status}."}, status=400
        )

    # Open window: the host opens the room once at least one non-host
    # invitee has accepted. There is no "join early" gate — the scheduled
    # date/time is a soft reminder. The duration timer starts on the
    # first physical join (which sets ``room_started_at`` below).
    #
    # Non-host invitees cannot start the room. They get a clear error
    # asking them to wait for the host until the host has opened it.
    now = timezone.now()
    if session.status == "scheduled":
        if not is_host:
            return Response(
                {"error": "Only the host can start this group session. "
                          "Please wait until the host opens the room."},
                status=400,
            )

        # Instant meetings skip the "at least 1 accepted invitee" gate —
        # there are no invitees at create-time. Google-Meet-style: the host
        # creates the room and walks straight in; participants join later
        # via the shareable link.
        if not is_instant:
            accepted_count = session.invites.filter(status="accepted").count()
            if accepted_count < 1:
                return Response(
                    {"error": "At least 1 invitee must accept before the room opens."},
                    status=400,
                )

        # Lock the row inside the atomic block so concurrent /join/ calls
        # from (somehow) two host clients can't both flip the status.
        # The second caller will see ``status != 'scheduled'`` and fall
        # through to the live-already branch below.
        with transaction.atomic():
            session = (
                GroupSession.objects
                .select_for_update()
                .get(pk=session.pk)
            )
            started_now = False
            if session.status == "scheduled":
                session.status = "live"
                session.room_name = f"group_session_{session.id}"
                session.room_started_at = now
                session.active_connections = 0
                session.all_left_at = None
                session.save(update_fields=[
                    "status", "room_name", "room_started_at",
                    "active_connections", "all_left_at", "updated_at",
                ])
                started_now = True
        if started_now:
            _schedule_hard_duration_cutoff(session)
            _broadcast(session)

    # Already live: check we're still within the duration.
    # ``cap_ends_at`` (set by extend_group_session) only ever pushes this
    # deadline LATER than the room's own configured duration_minutes — it's
    # never used to shrink it, so a session that never calls /extend/ behaves
    # exactly as before.
    if session.room_started_at:
        hard_end = session.room_started_at + timedelta(minutes=session.duration_minutes)
        if session.cap_ends_at and session.cap_ends_at > hard_end:
            hard_end = session.cap_ends_at
        if now >= hard_end:
            _end_group_session_internal(session, reason="duration_hit_on_join")
            _broadcast(session)
            return Response(
                {"error": "This group session has ended."}, status=400
            )

    if not session.room_name:
        return Response(
            {"error": "Room is not ready yet. Try again in a moment."}, status=400
        )

    # Admin-configurable room cap (GlobalSettings.live_max_participants),
    # separate from the pre-existing INSTANT_MAX_PARTICIPANTS check above
    # (which only applies to instant meetings and is hard-coded at 50). Host
    # is exempt, matching that same existing exemption. NOTE: this admin
    # default (40) is LOWER than an already-possible scheduled group session
    # (host + up to 50 invitees = 51). Flagged for a product decision before
    # this ships — either raise the default, or treat it as informational
    # only until admins are told to configure it per their real capacity.
    live_limits = live_rules.limits(session)
    live_limits["participants_now"] = session.active_connections or 0
    if not is_host and live_limits["participants_now"] >= live_limits["max_participants"]:
        return Response(
            {"detail": "This room is full.", "code": "room_full"},
            status=http_status.HTTP_409_CONFLICT,
        )

    try:
        display_name = get_user_name(user)
        role = "host" if is_host else ("teacher" if is_invited_teacher else "student")
        token = generate_group_session_token(
            user=user, session=session, display_name=display_name, role=role,
        )
    except Exception:
        logger.exception("LiveKit token generation failed for group session")
        return Response({"detail": "LiveKit error"}, status=500)

    if invite and not invite.joined_at:
        invite.joined_at = timezone.now()
        invite.save(update_fields=["joined_at"])

    # Best-effort "who's in the room" row — see GroupSessionParticipant's
    # docstring in models.py for exactly what this can/can't promise.
    GroupSessionParticipant.objects.update_or_create(
        session=session, user=user, defaults={"left_at": None}
    )

    # Compute remaining ms for client countdown.
    # Host is always unlimited (no entitlement check, by design). A non-host
    # who's entitled (enrolled, or platform is in free-launch mode) is also
    # unlimited. A non-entitled guest is capped at GUEST_TRIAL_MINUTES,
    # anchored to THEIR OWN first join (not each /join/ call's "now", and not
    # the room's own duration_minutes) so a refresh/reconnect can't reset the
    # clock and a room's own longer duration can't be used to bypass the cap.
    remaining_ms = None
    if not is_host and not _guest_entitlement(user):
        guest_session, _created = GroupSessionGuestSession.objects.get_or_create(
            session=session, user=user
        )
        guest_cap_end = guest_session.first_joined_at + timedelta(minutes=GUEST_TRIAL_MINUTES)
        room_hard_end = (
            session.room_started_at + timedelta(minutes=session.duration_minutes)
            if session.room_started_at else guest_cap_end
        )
        cap_end = min(guest_cap_end, room_hard_end)
        remaining_ms = max(0, int((cap_end - timezone.now()).total_seconds() * 1000))

    # ── Live-session rules enrichment (design_handoff_live_sessions) ──────
    # Additive only: every key below is NEW. The existing keys above (in
    # particular "role" — the in-room HOST/TEACHER/STUDENT badge — and
    # "remaining_ms" — the existing GUEST_TRIAL_MINUTES-anchored guest clock)
    # are left completely alone so old clients keep working unchanged. The
    # design handoff's own "role" (account role) and "remaining_ms"
    # (recomputed from GlobalSettings) would have collided with those two
    # existing keys under different semantics, so they were deliberately NOT
    # added under the same names — the equivalent information is available
    # to new clients via "entitlement" (free_minutes / minutes_used_today)
    # and "is_host" instead.
    ent = live_rules.entitlement(user, session)
    cap = live_rules.cap_ends_at(session) if session.room_started_at else None

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": role.upper(),
        "duration_minutes": session.duration_minutes,
        "room_started_at": session.room_started_at.isoformat() if session.room_started_at else None,
        "remaining_ms": remaining_ms,
        # ── new keys ──
        "is_host": is_host,
        "features": live_limits["features"],
        "limits": live_limits,
        "entitlement": ent,
        "cap_ends_at": cap.isoformat() if cap else None,
    })


# ===========================================================================
# LIVE-SESSION RULES  (design_handoff_live_sessions §4a/§4c)
# ===========================================================================


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_session_preflight(request, session_id):
    """Everything the pre-join lobby needs, in one call (screen 03)."""
    try:
        session = GroupSession.objects.select_related("host").get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    ent = live_rules.entitlement(request.user, session)
    lim = live_rules.limits(session)
    lim["participants_now"] = session.active_connections or 0
    cap = live_rules.cap_ends_at(session) if session.room_started_at else None

    return Response({
        "session": GroupSessionDetailSerializer(session).data,
        "host": {
            "id": session.host_id,
            "name": get_user_name(session.host),
            "is_teacher": session.host.has_role("TEACHER"),
            "in_room": session.participants.filter(
                user=session.host, left_at__isnull=True
            ).exists(),
        },
        "entitlement": ent,
        "limits": lim,
        # Real field is GroupSession.admit_mode ("open"/"lobby") — the
        # handoff's doc assumed a boolean ``require_approval`` that doesn't
        # exist on this model.
        "admit_mode": session.admit_mode,
        "can_host": live_rules.can_host(request.user),
        "is_enrolled": live_rules.is_enrolled_for(request.user, session),
        "cap_ends_at": cap.isoformat() if cap else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def extend_group_session(request, session_id):
    """Host adds time, bounded by the admin cap (screen 07).

    Deliberately does NOT reuse the existing ``_schedule_hard_duration_cutoff``
    helper — that helper's eta is hard-coded to
    ``room_started_at + duration_minutes`` with no notion of extensions, and
    changing it in place would retroactively apply the admin's
    ``live_max_session_minutes`` ceiling (default 90) to every existing
    session type, including 3-hour instant meetings, which would be a real
    regression. Instead this schedules its own eta directly from the newly
    computed ``cap_ends_at``, which is itself floored at the room's original
    duration-based cutoff (see the comment at the "already live" check in
    ``join_group_session``) so it only ever pushes the deadline later.
    """
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if request.user.id != session.host_id:
        return Response({"detail": "Only the host can extend."}, status=403)
    if session.status != "live" or not session.room_started_at:
        return Response({"detail": "Session is not live."}, status=400)

    s = live_rules.rules()
    if session.extensions_used >= s.live_host_extensions_allowed:
        return Response(
            {"detail": "No extensions left.", "code": "extensions_exhausted"},
            status=409,
        )

    session.extensions_used += 1
    baseline_end = session.room_started_at + timedelta(minutes=session.duration_minutes)
    session.cap_ends_at = max(live_rules.cap_ends_at(session), baseline_end)
    session.save(update_fields=["extensions_used", "cap_ends_at", "updated_at"])

    try:
        from .group_session_tasks import hard_expire_group_session
        hard_expire_group_session.apply_async(
            args=[str(session.id)], eta=session.cap_ends_at
        )
    except Exception:
        logger.exception(
            "Failed to reschedule hard-duration cutoff for %s", session.id
        )

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"group_session_chat_{session.id}",
                {
                    "type": "session_extended",
                    "cap_ends_at": session.cap_ends_at.isoformat(),
                    "extensions_used": session.extensions_used,
                    "extensions_allowed": s.live_host_extensions_allowed,
                },
            )
        except Exception:
            pass

    return Response({
        "cap_ends_at": session.cap_ends_at,
        "extensions_used": session.extensions_used,
        "extensions_allowed": s.live_host_extensions_allowed,
    })


# ===========================================================================
# CHAT ENDPOINTS  (group-session rooms only)
#
# Mirrors private-session chat (views.session_chat_messages /
# views.send_chat_message) but writes to GroupSessionChatMessage.  Auth gate
# allows the host plus any accepted invitee.  Storage is purged the moment
# the session ends — see _end_group_session_internal which bulk-deletes
# GroupSessionChatMessage rows for that session.
# ===========================================================================


def _chat_participant_check(session, user):
    """
    Return (allowed, error_response_or_None).
    A user may chat in a group-session room iff:
      * they are the host, OR
      * they have an 'accepted' invite, OR
      * it's an instant meeting (anyone with the link is a participant).
    """
    if session.host_id == user.id:
        return True, None
    if getattr(session, "session_type", "") == "instant":
        return True, None
    invite = session.invites.filter(user=user).first()
    if invite and invite.status == "accepted":
        return True, None
    return False, Response(
        {"error": "Not a participant."},
        status=http_status.HTTP_403_FORBIDDEN,
    )


def _serialize_sg_chat_message(msg):
    return {
        "id": str(msg.id),
        "sender_id": str(msg.sender_id),
        "sender_name": msg.sender_name,
        "sender_role": msg.sender_role,
        "message": msg.message,
        "created_at": msg.created_at.isoformat(),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_session_chat_messages(request, session_id):
    """Return up to the last 200 chat messages for a group-session session."""
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    allowed, err = _chat_participant_check(session, request.user)
    if not allowed:
        return err

    msgs = (
        GroupSessionChatMessage.objects
        .filter(session=session)
        .order_by("created_at")[:200]
    )
    return Response([_serialize_sg_chat_message(m) for m in msgs])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_group_session_chat_message(request, session_id):
    """Persist a chat message and broadcast it to all WS clients in the room."""
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    allowed, err = _chat_participant_check(session, request.user)
    if not allowed:
        return err

    if session.status != "live":
        # Mirrors private-session behaviour — chat only while the room is open.
        return Response(
            {"error": "Chat is only available while the room is live."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    text = (request.data.get("message") or "").strip()
    if not text:
        return Response(
            {"error": "Message cannot be empty."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if len(text) > 2000:
        return Response(
            {"error": "Message too long (max 2000 chars)."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    user = request.user
    is_host = (session.host_id == user.id)
    is_invited_teacher = bool(
        session.invited_teacher_id and session.invited_teacher_id == user.id
    )
    role = "host" if is_host else ("teacher" if is_invited_teacher else "student")

    msg = GroupSessionChatMessage.objects.create(
        session=session,
        sender=user,
        sender_name=get_user_name(user),
        sender_role=role,
        message=text,
    )
    payload = _serialize_sg_chat_message(msg)

    # Fan-out to the consumer group; every connected client gets it.
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"group_session_chat_{session.id}",
                {"type": "chat_message", "data": payload},
            )
        except Exception:
            logger.exception("Channel-layer broadcast failed for group session %s", session.id)

    return Response(payload, status=http_status.HTTP_201_CREATED)


# ===========================================================================
# NOTES — private per-user scratchpad, no review counterpart (spec: group
# sessions never show a post-call review modal).
# ===========================================================================


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def group_session_note(request, session_id):
    """GET/PATCH group-sessions/<session_id>/notes/ — the requesting user's
    own private notes for this session. Uses the same participant gate as
    chat (host / accepted invite / instant meeting) rather than the looser
    `_can_view` used for read-only session info, since this is a
    write-capable per-user resource.
    """
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    allowed, err = _chat_participant_check(session, request.user)
    if not allowed:
        return err

    if request.method == "GET":
        note = GroupSessionNote.objects.filter(session=session, user=request.user).first()
        return Response(
            GroupSessionNoteSerializer(note).data if note else {"content": "", "updated_at": None}
        )

    serializer = GroupSessionNoteSerializer(data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)

    note, _ = GroupSessionNote.objects.update_or_create(
        session=session,
        user=request.user,
        defaults=serializer.validated_data,
    )

    return Response(GroupSessionNoteSerializer(note).data)


# ===========================================================================
# INSTANT MEETING + END SESSION + ADMIT MODE
# ===========================================================================

import secrets as _secrets


def _generate_short_code():
    """Return a Google-Meet-style 'xxx-yyyy-zzz' code unique in the DB."""
    alphabet = "abcdefghijkmnpqrstuvwxyz"
    for _ in range(8):
        a = "".join(_secrets.choice(alphabet) for _ in range(3))
        b = "".join(_secrets.choice(alphabet) for _ in range(4))
        c = "".join(_secrets.choice(alphabet) for _ in range(3))
        code = f"{a}-{b}-{c}"
        if not GroupSession.objects.filter(short_code=code).exists():
            return code
    import uuid as _uuid
    return f"gs-{_uuid.uuid4().hex[:10]}"


def _is_paid_user(user):
    """Paywall stub. Returns True for any authenticated user for now."""
    if not user or not user.is_authenticated:
        return False
    explicit = getattr(user, "is_paid", None)
    if explicit is not None:
        return bool(explicit)
    return True


GUEST_TRIAL_MINUTES = 15


def _guest_entitlement(user):
    """
    True = unlimited time in a group session; False = capped at
    GUEST_TRIAL_MINUTES. Deliberately separate from ``_is_paid_user`` — that
    stub still gates whether someone may join/host at all; this only decides
    the *duration* for a non-host joiner, and must never gate the host path
    (the host is always unlimited, per spec, regardless of their own status).
    """
    if GlobalSettings.load().effective_mode == GlobalSettings.PAYMENT_FREE:
        # Whole platform is in a free-launch phase — the enrolled/not-enrolled
        # distinction is moot, so nobody is capped.
        return True
    return Enrollment.objects.filter(
        status=Enrollment.STATUS_ACTIVE
    ).filter(
        Q(user=user) | Q(learner_profile__account=user)
    ).exists()


def _broadcast_session_ended(session, reason="ended"):
    """Push a session_ended event to the live-room WS group."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"group_session_chat_{session.id}",
            {
                "type": "session_ended",
                "data": {
                    "session_id": str(session.id),
                    "reason": reason,
                    "ended_at": timezone.now().isoformat(),
                },
            },
        )
    except Exception:
        logger.exception("session_ended broadcast failed for %s", session.id)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def instant_create(request):
    """One-click Instant Meeting; allowed for any authenticated paid user."""
    if not _is_paid_user(request.user):
        return Response(
            {"error": "Your account is not eligible to start meetings."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    now = timezone.now()
    local_now = timezone.localtime(now)
    duration_minutes = int(request.data.get("duration_minutes") or 60)
    if duration_minutes not in {30, 45, 60, 90, 120, 150, 180}:
        duration_minutes = 60

    topic = (request.data.get("topic") or "").strip()[:255]

    with transaction.atomic():
        session = GroupSession.objects.create(
            host=request.user,
            invited_teacher=None,
            subject=None,
            subject_name="",
            course_title="",
            topic=topic or "Instant meeting",
            # scheduled_date/scheduled_time are IST-calendar fields (every
            # other reader of this session combines them back via
            # _scheduled_aware_dt()'s make_aware(), which assumes local
            # time) — using the raw UTC `now` here shifts the displayed
            # date/time by 5.5h and flips the date during 18:30-23:59 UTC
            # (00:00-05:29 IST).
            scheduled_date=local_now.date(),
            scheduled_time=local_now.time().replace(microsecond=0),
            duration_minutes=duration_minutes,
            session_type="instant",
            admit_mode="open",
            # Instant rooms open immediately — the host is dropped
            # straight into a live room without the "at least 1 invitee
            # must accept" gate that scheduled group sessions enforce.
            status="live",
            short_code=_generate_short_code(),
        )
        # Set the LiveKit room name + room_started_at at create time
        # so the very first /join/ call goes straight to token issuance.
        session.room_name = f"group_session_{session.id}"
        session.room_started_at = now
        session.active_connections = 0
        session.all_left_at = None
        session.save(update_fields=[
            "room_name", "room_started_at", "active_connections",
            "all_left_at", "status", "updated_at",
        ])
        _schedule_hard_duration_cutoff(session)

    full = _gs_qs().get(pk=session.pk)
    return Response(
        GroupSessionDetailSerializer(full).data,
        status=http_status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def end_group_session(request, session_id):
    """Host-only: hard-end the room."""
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.host_id != request.user.id:
        return Response(
            {"error": "Only the host can end this session."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    if session.status in {"completed", "cancelled", "expired"}:
        return Response(
            {"status": session.status, "ended_at": session.ended_at},
            status=http_status.HTTP_200_OK,
        )

    now = timezone.now()
    with transaction.atomic():
        session.status = "completed"
        session.ended_at = now
        session.all_left_at = now
        session.active_connections = 0
        session.save(update_fields=[
            "status", "ended_at", "all_left_at", "active_connections", "updated_at",
        ])
        if session.session_type == "instant":
            GroupSessionChatMessage.objects.filter(session=session).delete()

    _broadcast_session_ended(session, reason="host_ended")

    return Response(
        {"status": session.status, "ended_at": session.ended_at.isoformat()},
        status=http_status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_admit_mode(request, session_id):
    """Host-only: toggle the room between 'open' and 'lobby' admit modes.

    When 'lobby', join_group_session holds any non-host caller in a
    GroupSessionJoinRequest ('pending') instead of issuing a token, until
    the host admits them via admit_join_request.
    """
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.host_id != request.user.id:
        return Response(
            {"error": "Only the host can change admit mode."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    mode = (request.data.get("admit_mode") or "").strip().lower()
    if mode not in {"open", "lobby"}:
        return Response(
            {"error": "admit_mode must be 'open' or 'lobby'."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    session.admit_mode = mode
    session.save(update_fields=["admit_mode", "updated_at"])
    return Response({"admit_mode": session.admit_mode}, status=http_status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Knock-to-join: the waiting guest polls my_join_status/ (no LiveKit room to
# receive a data-channel nudge on yet); the host's classroom UI polls
# join_requests/ and calls admit/deny. Polling over a WS consumer, deliberately
# — admit/deny is low-frequency and a ~2s poll is invisible for a one-time
# approval.
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_join_status(request, session_id):
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.host_id == request.user.id:
        return Response({"status": "admitted"})

    join_req = GroupSessionJoinRequest.objects.filter(
        session=session, user=request.user
    ).first()
    if not join_req:
        return Response({"status": "admitted"})  # no lobby gate applies to this caller
    return Response({"status": join_req.status, "deny_message": join_req.deny_message})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_join_requests(request, session_id):
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.host_id != request.user.id:
        return Response(
            {"error": "Only the host can view join requests."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    pending = session.join_requests.filter(status="pending").select_related("user")
    return Response([
        {"id": str(r.id), "user_id": str(r.user_id), "name": get_user_name(r.user),
         "requested_at": r.requested_at.isoformat()}
        for r in pending
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admit_join_request(request, session_id, request_id):
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)
    if session.host_id != request.user.id:
        return Response({"error": "Only the host can admit."}, status=http_status.HTTP_403_FORBIDDEN)

    join_req = GroupSessionJoinRequest.objects.filter(session=session, pk=request_id).first()
    if not join_req:
        return Response({"error": "Join request not found."}, status=404)

    join_req.status = "admitted"
    join_req.resolved_at = timezone.now()
    join_req.save(update_fields=["status", "resolved_at"])
    return Response({"status": "admitted"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def deny_join_request(request, session_id, request_id):
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)
    if session.host_id != request.user.id:
        return Response({"error": "Only the host can deny."}, status=http_status.HTTP_403_FORBIDDEN)

    join_req = GroupSessionJoinRequest.objects.filter(session=session, pk=request_id).first()
    if not join_req:
        return Response({"error": "Join request not found."}, status=404)

    join_req.status = "denied"
    join_req.resolved_at = timezone.now()
    join_req.deny_message = (request.data.get("message") or "").strip()[:255]
    join_req.save(update_fields=["status", "resolved_at", "deny_message"])
    return Response({"status": "denied"})


# ---------------------------------------------------------------------------
# Join by code — look up a session by its short_code (or UUID) and return
# enough detail for the frontend to navigate to /group-session/live/<id>.
# Authentication is enforced via IsAuthenticated; the paywall stub still
# applies. Token issuance and the room-open side effects still happen in
# /join/ — this endpoint is just a lookup so the user can paste a code
# instead of a full URL.
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_by_code(request):
    """Resolve a room code (or UUID) to a GroupSession and return its id.

    Request body:  { "code": "xyz-abcd-efg" }    or    { "code": "<uuid>" }

    Responses:
      200 { session_id, short_code, status, session_type, host_id }
      400 if code missing / malformed
      403 if user is not entitled (paywall stub)
      404 if no session matches OR if it's already terminal (so a stale
          link doesn't drop the joiner into a dead UUID).
    """
    if not _is_paid_user(request.user):
        return Response(
            {"error": "Your account is not eligible to join meetings."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    raw = (request.data.get("code") or "").strip()
    if not raw:
        return Response(
            {"error": "A room code is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    # Normalize: codes are lowercase; full URLs get reduced to the last path
    # segment so users can paste either format.
    code = raw.lower()
    if "/" in code:
        code = code.rstrip("/").split("/")[-1]

    # Try short_code first, then fall back to UUID lookup (so older sessions
    # without a short_code can still be joined by full id from the URL).
    session = GroupSession.objects.filter(short_code=code).first()
    if session is None:
        import uuid as _uuid
        try:
            uid = _uuid.UUID(code)
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            session = GroupSession.objects.filter(pk=uid).first()

    if session is None:
        return Response(
            {"error": "No room found for that code."},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    if session.status in ("cancelled", "completed", "expired"):
        return Response(
            {"error": f"This room is {session.status} and can no longer be joined."},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    return Response({
        "session_id": str(session.id),
        "short_code": session.short_code,
        "status": session.status,
        "session_type": session.session_type,
        "host_id": str(session.host_id) if session.host_id else None,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_group_session_list(request):
    """GET /sessions/admin/group-sessions/  → every group session with real
    per-user attendance (join/leave/watch-seconds), for the Admin-dashboard.
    This data has existed since the LiveKit webhook attendance pipeline was
    built for livestream/group sessions — this endpoint is the first thing
    to actually surface it in the admin app.
    """
    qs = (GroupSession.objects
          .select_related("host")
          .prefetch_related(
              Prefetch("attendances",
                       queryset=GroupSessionAttendance.objects.select_related("user"))
          )
          .order_by("-created_at")[:100])

    rows = []
    for gs in qs:
        attendance = [
            {
                "user_id": str(a.user_id),
                "name": get_user_name(a.user),
                "joined_at": a.joined_at,
                "left_at": a.left_at,
                "total_seconds": a.total_seconds,
            }
            for a in gs.attendances.all()
        ]
        rows.append({
            "id": str(gs.id),
            "topic": gs.topic or gs.subject_name or "Group session",
            "host": get_user_name(gs.host) if gs.host else None,
            "session_type": gs.session_type,
            "status": gs.status,
            "scheduled_date": gs.scheduled_date,
            "scheduled_time": gs.scheduled_time,
            "attendance": attendance,
        })
    return Response({"sessions": rows})


# ===========================================================================
# POST-SESSION SUMMARY + REVIEW  (design_handoff_live_sessions Phase 5,
# screen 09 — 01-FLOW.md section F)
# ===========================================================================
# 01-FLOW.md §F assumed ``GET /sessions/group-sessions/:id/summary/`` and a
# review POST to "the existing SessionReview endpoint" both already existed.
# Neither did: there was no summary endpoint anywhere in this file, and the
# only real ``SessionReview`` model belongs to the unrelated ``livestream``
# app (LiveSession, not GroupSession) — see GroupSessionReview's docstring in
# models.py for the full explanation. Both are added here, additively; no
# existing endpoint's behaviour changes.


def _file_summary(f, request):
    """Same shape as live_files_views.py's own ``_serialize`` — kept as a
    separate function (not imported) so this view has no dependency on that
    module's internals, but the field names match exactly so the frontend's
    FilesPanel-style row rendering works unmodified against either response.
    """
    return {
        "id": f.id,
        "name": f.original_name,
        "url": request.build_absolute_uri(f.file.url),
        "size_bytes": f.size_bytes,
        "content_type": f.content_type,
        "uploaded_by": f.uploaded_by.get_full_name() or f.uploaded_by.username,
        "uploaded_by_id": f.uploaded_by_id,
        "created_at": f.created_at,
        "expires_at": f.expires_at,
        "saved_to_course": f.saved_to_course,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_session_summary(request, session_id):
    """GET /group-sessions/<id>/summary/ — everything screen 09 needs.

    Access mirrors every other read-only group-session endpoint: host,
    invited teacher, any invitee, or (for instant rooms) any authenticated
    user — see ``_can_view``. Deliberately NOT gated on
    ``session.status == "completed"``: a participant who was just
    disconnected by the T-0 ending-soon cutoff (or who hit "Leave") lands
    here immediately, often before the room's own status flip / attendance
    webhook has finished landing, so every stat below tolerates a
    still-``live`` session and missing/partial attendance rows rather than
    404ing.
    """
    try:
        session = GroupSession.objects.select_related(
            "host", "subject", "subject__course__board"
        ).get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this group session."}, status=403
        )

    # ── your own attendance (duration attended) ──
    my_attendance = GroupSessionAttendance.objects.filter(
        session=session, user=request.user
    ).first()
    my_seconds = my_attendance.total_seconds if my_attendance else 0
    # Host/others who never accumulated a rollup row yet (e.g. summary
    # loaded seconds after redirect, before the webhook lands) still get a
    # sane floor from room_started_at, so "0m" doesn't flash for the host.
    if not my_seconds and session.room_started_at:
        end = session.ended_at or timezone.now()
        my_seconds = max(0, int((end - session.room_started_at).total_seconds()))

    # ── participants (everyone with an attendance rollup, i.e. actually
    #    attended — not just "currently connected") ──
    attendance_rows = (
        GroupSessionAttendance.objects.filter(session=session)
        .select_related("user")
    )
    participants = [
        {
            "user_id": str(a.user_id),
            "name": get_user_name(a.user),
            "is_host": a.user_id == session.host_id,
            "total_seconds": a.total_seconds,
        }
        for a in attendance_rows
    ]
    if session.host_id and not any(p["is_host"] for p in participants):
        participants.insert(0, {
            "user_id": str(session.host_id),
            "name": get_user_name(session.host),
            "is_host": True,
            "total_seconds": my_seconds if session.host_id == request.user.id else 0,
        })

    # ── files (with expiry) ──
    files = [
        _file_summary(f, request)
        for f in session.files.select_related("uploaded_by")
    ]

    # ── remote-assist count — grants that actually activated, not just
    #    requested/declined ──
    remote_assist_count = RemoteControlGrant.objects.filter(
        session=session, granted_at__isnull=False
    ).count()

    # ── your private note ──
    my_note = GroupSessionNote.objects.filter(
        session=session, user=request.user
    ).first()

    # ── your existing review, if you already submitted one ──
    my_review = GroupSessionReview.objects.filter(
        session=session, user=request.user
    ).first()

    return Response({
        "session": GroupSessionDetailSerializer(session).data,
        "you_attended_seconds": my_seconds,
        "participants": participants,
        "participants_count": len(participants),
        "files": files,
        "files_count": len(files),
        "remote_assist_count": remote_assist_count,
        "my_note": my_note.content if my_note else "",
        "chat_path": f"/sessions/group-sessions/{session.id}/chat/",
        "my_review": GroupSessionReviewSerializer(my_review).data if my_review else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_group_session_review(request, session_id):
    """POST /group-sessions/<id>/review/ — 1-5 star rating + optional
    description from the summary screen's "How was the session?" card.

    Mirrors ``submit_private_session_review`` exactly: not gated on
    ``session.status``, and resubmitting overwrites via
    ``update_or_create`` rather than erroring on the unique constraint.
    """
    try:
        session = GroupSession.objects.get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this group session."}, status=403
        )

    serializer = GroupSessionReviewSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    review, _ = GroupSessionReview.objects.update_or_create(
        session=session,
        user=request.user,
        defaults=serializer.validated_data,
    )

    return Response(
        GroupSessionReviewSerializer(review).data, status=http_status.HTTP_201_CREATED
    )
