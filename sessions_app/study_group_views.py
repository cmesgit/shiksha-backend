"""
Study Group API endpoints.

All endpoints for the new Study Group feature live here so the existing
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

from .models import StudyGroupSession, StudyGroupInvite
from .permissions import IsStudent
from .serializers import get_user_name
from .services.study_group_token import generate_study_group_token
from .study_group_serializers import (
    StudyGroupCreateSerializer,
    StudyGroupDetailSerializer,
    StudyGroupInviteMoreSerializer,
    StudyGroupListSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sg_qs():
    """Base queryset with everything needed by the list serializer."""
    return (
        StudyGroupSession.objects.select_related(
            "host", "host__profile",
            "invited_teacher", "invited_teacher__profile",
            "subject", "subject__course",
        )
        .prefetch_related(
            Prefetch(
                "invites",
                queryset=StudyGroupInvite.objects.select_related(
                    "user", "user__profile"
                ),
            )
        )
    )


def _can_view(session, user):
    """A session is visible to host, invited teacher, or any invitee."""
    if session.host_id == user.id:
        return True
    if session.invited_teacher_id and session.invited_teacher_id == user.id:
        return True
    return session.invites.filter(user=user).exists()


def _scheduled_aware_dt(session):
    """Return the tz-aware scheduled start datetime for a study group.

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
                "is_study_group": True,
            })
    except Exception:
        logger.exception("Failed to push study-group notification")


def _broadcast(session):
    """Push list-shape session_update to host + all invited users."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    # Re-fetch with prefetches so counts are correct
    full = _sg_qs().get(pk=session.pk)
    data = StudyGroupListSerializer(full).data

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


def _end_study_group_internal(session, reason="ended"):
    """Finalise a live session. Used by hard-duration task, idle cleanup, and cancel-live."""
    if session.status != "live":
        return False
    session.status = "completed"
    session.ended_at = timezone.now()
    session.save(update_fields=["status", "ended_at", "updated_at"])
    logger.info("StudyGroup %s ended (reason: %s)", session.id, reason)
    return True


def _schedule_hard_duration_cutoff(session):
    """Queue a Celery task that force-ends the room at duration expiry."""
    try:
        from .study_group_tasks import hard_expire_study_group
        eta = session.room_started_at + timedelta(minutes=session.duration_minutes)
        hard_expire_study_group.apply_async(args=[str(session.id)], eta=eta)
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
def create_study_group(request):
    ser = StudyGroupCreateSerializer(data=request.data)
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
        session = StudyGroupSession.objects.create(
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
            invites.append(StudyGroupInvite(
                session=session, user_id=uid, invite_role="student",
            ))
        if invited_teacher:
            invites.append(StudyGroupInvite(
                session=session, user_id=invited_teacher.id, invite_role="teacher",
            ))
        StudyGroupInvite.objects.bulk_create(invites)

    # ── Notify each invitee ──────────────────────────────────────────
    host_name = get_user_name(request.user)
    for inv in StudyGroupInvite.objects.filter(session=session).select_related("user"):
        if inv.invite_role == "teacher":
            title = f"📚 {host_name} invited you to a {session.subject_name} study group"
        else:
            title = f"📚 {host_name} invited you to a {session.subject_name} study group"
        _notify_user(inv.user, title, session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def invite_more(request, session_id):
    """Add more invitees after the fact (host only, while status=scheduled)."""
    try:
        session = StudyGroupSession.objects.select_related(
            "subject", "subject__course"
        ).get(pk=session_id, host=request.user)
    except StudyGroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.status != "scheduled":
        return Response(
            {"error": "Can only invite more while the group is scheduled."},
            status=400,
        )

    ser = StudyGroupInviteMoreSerializer(data=request.data)
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

    StudyGroupInvite.objects.bulk_create([
        StudyGroupInvite(session=session, user_id=uid, invite_role="student")
        for uid in to_add_ids
    ])

    host_name = get_user_name(request.user)
    for inv in session.invites.filter(user_id__in=to_add_ids).select_related("user"):
        _notify_user(
            inv.user,
            f"📚 {host_name} invited you to a {session.subject_name} study group",
            session,
        )

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Invitee responses
# ---------------------------------------------------------------------------


def _get_invite_for_user(session_id, user):
    return (
        StudyGroupInvite.objects.select_related(
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
            {"error": "This study group's start time has passed; you can no longer respond."},
            status=400,
        )

    invite.status = "accepted"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    # Notify the host (the user who initiated the study group request).
    # Use a slightly different copy when a TEACHER accepts, so the host
    # knows the room can already be opened on their authority.
    responder_label = "Teacher" if invite.invite_role == "teacher" else ""
    actor_name = get_user_name(request.user)
    if responder_label:
        title = (
            f"✅ {responder_label} {actor_name} accepted your "
            f"{session.subject_name} study group"
        )
    else:
        title = (
            f"✅ {actor_name} accepted your {session.subject_name} study group"
        )
    _notify_user(session.host, title, session)
    _broadcast(session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


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
            {"error": "This study group's start time has passed; you can no longer respond."},
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
            f"{session.subject_name} study group"
        )
    else:
        title = (
            f"↩ {actor_name} declined your {session.subject_name} study group"
        )
    _notify_user(session.host, title, session)
    _broadcast(session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def reinvite(request, session_id):
    """Host re-invites a single user who previously declined (allowed once)."""
    try:
        session = StudyGroupSession.objects.get(pk=session_id, host=request.user)
    except StudyGroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if session.status != "scheduled":
        return Response(
            {"error": "Can only re-invite while scheduled."}, status=400
        )

    # After the start time has passed, the card is read-only on both sides.
    if _scheduled_aware_dt(session) <= timezone.now():
        return Response(
            {"error": "This study group's start time has passed; you can no longer re-invite."},
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
        f"📚 {host_name} re-invited you to their {session.subject_name} study group",
        session,
    )
    _broadcast(session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


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
            {"error": "This study group's start time has passed; you can no longer change your response."},
            status=400,
        )

    invite.status = "pending"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    # Let the host know someone just stepped back.
    _notify_user(
        session.host,
        f"↩ {get_user_name(request.user)} is no longer attending your {session.subject_name} study group",
        session,
    )
    _broadcast(session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


# ---------------------------------------------------------------------------
# Cancel / listing / detail
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsStudent])
def cancel_study_group(request, session_id):
    try:
        session = StudyGroupSession.objects.get(pk=session_id, host=request.user)
    except StudyGroupSession.DoesNotExist:
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
            f"❌ {host_name} cancelled the {session.subject_name} study group",
            session,
        )
    _broadcast(session)

    full = _sg_qs().get(pk=session.pk)
    return Response(StudyGroupDetailSerializer(full).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_study_groups(request):
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

    base = _sg_qs()

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
        )
    else:  # upcoming (default)
        qs = base.filter(
            Q(host=user)
            | Q(invites__user=user, invites__status="accepted")
            | Q(invited_teacher=user, invites__user=user, invites__status="accepted"),
            status__in=["scheduled", "live"],
        ).exclude(past_orphan_q)

    qs = qs.distinct().order_by("scheduled_date", "scheduled_time")
    return Response(StudyGroupListSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def study_group_detail(request, session_id):
    try:
        session = _sg_qs().get(pk=session_id)
    except StudyGroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    if not _can_view(session, request.user):
        return Response(
            {"error": "You do not have access to this study group."}, status=403
        )
    return Response(StudyGroupDetailSerializer(session).data)


# ---------------------------------------------------------------------------
# Join (LiveKit token) — opens the room on first join
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_study_group(request, session_id):
    """
    Returns a LiveKit token if the caller may join.

    Side-effects on first join:
      * flips status from scheduled → live
      * assigns room_name + room_started_at
      * schedules a Celery task at room_started_at + duration for the
        hard-duration cutoff.
    """
    try:
        session = StudyGroupSession.objects.select_for_update().get(pk=session_id)
    except StudyGroupSession.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    user = request.user

    # Auth check.
    #
    # Roles in a study group:
    #   * Host:  the student who created the group. Implicitly accepted —
    #            no invite row exists for them. Only the host may flip the
    #            status from scheduled → live (start the room).
    #   * Invited teacher / invited student: must explicitly accept their
    #            own invite before they may join. They cannot start the
    #            room; they wait until the host opens it.
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
    elif invite is None:
        return Response(
            {"error": "You are not a participant in this study group."},
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
                {"error": "Only the host can start this study group. "
                          "Please wait until the host opens the room."},
                status=400,
            )

        accepted_count = session.invites.filter(status="accepted").count()
        if accepted_count < 1:
            return Response(
                {"error": "At least 1 invitee must accept before the room opens."},
                status=400,
            )

        with transaction.atomic():
            session.status = "live"
            session.room_name = f"study_group_{session.id}"
            session.room_started_at = now
            session.active_connections = 0
            session.all_left_at = None
            session.save(update_fields=[
                "status", "room_name", "room_started_at",
                "active_connections", "all_left_at", "updated_at",
            ])
        _schedule_hard_duration_cutoff(session)
        _broadcast(session)

    # Already live: check we're still within the duration
    if session.room_started_at:
        hard_end = session.room_started_at + timedelta(minutes=session.duration_minutes)
        if now >= hard_end:
            _end_study_group_internal(session, reason="duration_hit_on_join")
            _broadcast(session)
            return Response(
                {"error": "This study group has ended."}, status=400
            )

    if not session.room_name:
        return Response(
            {"error": "Room is not ready yet. Try again in a moment."}, status=400
        )

    try:
        display_name = get_user_name(user)
        role = "host" if is_host else ("teacher" if is_invited_teacher else "student")
        token = generate_study_group_token(
            user=user, session=session, display_name=display_name, role=role,
        )
    except Exception:
        logger.exception("LiveKit token generation failed for study group")
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
