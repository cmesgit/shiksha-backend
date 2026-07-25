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

from .models import GroupSession, GroupSessionInvite, GroupSessionChatMessage, GroupSessionNote
from .permissions import IsStudent
from .serializers import get_user_name
from .services.group_session_token import generate_group_session_token
from .group_session_serializers import (
    GroupSessionNoteSerializer,
    GroupSessionCreateSerializer,
    GroupSessionDetailSerializer,
    GroupSessionInviteMoreSerializer,
    GroupSessionListSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gs_qs():
    """Base queryset with everything needed by the list serializer."""
    return (
        GroupSession.objects.select_related(
            "host",
            "invited_teacher",
            "subject", "subject__course",
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


def _notify_user(user, title, session):
    """Create an Activity row + push a bell notification to ``user``.

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
            },
        )
        if created:
            push_ws_notification(user.id, {
                "type": "SESSION",
                "title": title,
                "subject_name": session.subject_name,
                "id": str(session.id),
                "is_read": False,
                "created_at": activity.created_at.isoformat(),
                "is_group_session": True,
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
    ).select_related("course", "course__stream")

    out = []
    for enr in enrollments:
        course = enr.course
        course_label = course.title
        if course.stream:
            course_label = f"{course.title} — {course.stream.name.title()}"
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

    from courses.models import Subject, SubjectTeacher
    from enrollments.models import Enrollment

    # ── Validate subject + enrollment ────────────────────────────────
    try:
        subject = Subject.objects.select_related("course", "course__stream").get(
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
        if not SubjectTeacher.objects.filter(
            subject=subject, teacher_id=invited_teacher_id
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
    course_label = subject.course.title
    if subject.course.stream:
        course_label = f"{subject.course.title} — {subject.course.stream.name.title()}"

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
        _notify_user(inv.user, title, session)

    full = _gs_qs().get(pk=session.pk)
    return Response(GroupSessionDetailSerializer(full).data, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def invite_more(request, session_id):
    """Add more invitees after the fact (host only, while status=scheduled)."""
    try:
        session = GroupSession.objects.select_related(
            "subject", "subject__course"
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_session_detail(request, session_id):
    try:
        session = _gs_qs().get(pk=session_id)
    except GroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this group session."}, status=403
        )
    return Response(GroupSessionDetailSerializer(session).data)


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
        # Open join for instant meetings — auth + paywall above are the only gates.
        # (Admit-mode='lobby' is a Phase-2 addition; until then 'open' is enforced.)
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
    # even if it's "full".
    if is_instant and not is_host:
        if (session.active_connections or 0) >= INSTANT_MAX_PARTICIPANTS:
            return Response(
                {"error": f"This instant meeting is full "
                          f"({INSTANT_MAX_PARTICIPANTS} participants max). "
                          f"Please try again once someone leaves."},
                status=http_status.HTTP_403_FORBIDDEN,
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

    # Already live: check we're still within the duration
    if session.room_started_at:
        hard_end = session.room_started_at + timedelta(minutes=session.duration_minutes)
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

    # Compute remaining ms for client countdown
    remaining_ms = None
    if session.room_started_at:
        hard_end = session.room_started_at + timedelta(minutes=session.duration_minutes)
        remaining_ms = max(0, int((hard_end - timezone.now()).total_seconds() * 1000))

    return Response({
        "livekit_url": settings.LIVEKIT_URL,
        "token": token,
        "room": session.room_name,
        "role": role.upper(),
        "duration_minutes": session.duration_minutes,
        "room_started_at": session.room_started_at.isoformat() if session.room_started_at else None,
        "remaining_ms": remaining_ms,
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
            scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
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

    NOTE: the lobby (knock-to-enter) flow is not fully wired through to
    the join handler yet — this endpoint persists the field so the host
    UI toggle is functional, but join_group_session still admits all
    accepted invitees. The lobby gate is a Phase-2 addition.
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
