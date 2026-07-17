# PLACEMENT: skills/livekit_views.py  (replace the whole file)
# slot back to the expert's open grid (matches the existing decline behaviour).
"""
skills/livekit_views.py — LiveKit room token generation and session management.

Settings required in settings_base.py:
    LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY", "devkey")
    LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "devsecret")
    LIVEKIT_WS_URL     = os.getenv("LIVEKIT_WS_URL", "ws://localhost:7880")

Install: pip install livekit

Routes (wired in skills/urls.py):
    POST /skill/sessions/<id>/join/      → get LiveKit token
    GET  /skill/my-sessions/             → student's active sessions
    GET  /skill/teacher/sessions/        → teacher's session queue
    POST /skill/teacher/sessions/<id>/confirm/   → teacher confirms
    POST /skill/teacher/sessions/<id>/complete/  → teacher marks complete
"""
from django.conf import settings
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework import status as drf_status

from accounts.auth_flow import get_active_profile
from .models import SkillSession, ExpertProfile
from .notifications import push_skill_bell


def _make_token(identity: str, room_name: str, *, can_publish: bool = True) -> str:
    """
    Generate a LiveKit access token.
    Falls back to a stub string if the livekit-api package isn't installed yet —
    so the endpoint keeps working in dev even before `pip install livekit-api`.
    """
    try:
        # AccessToken / VideoGrants live in `livekit.api` (the livekit-api
        # package), NOT the top-level `livekit` rtc package. Importing them
        # from `livekit` directly raises ImportError even when installed,
        # which previously made this always return the stub.
        from livekit.api import AccessToken, VideoGrants
        token = (
            AccessToken(
                getattr(settings, "LIVEKIT_API_KEY", "devkey"),
                getattr(settings, "LIVEKIT_API_SECRET", "devsecret"),
            )
            .with_identity(identity)
            .with_grants(VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=can_publish,
                can_subscribe=True,
            ))
            .to_jwt()
        )
        return token
    except ImportError:
        # livekit-api not installed — return a clear placeholder.
        return f"livekit-stub::room={room_name}::identity={identity}"


class JoinSessionView(APIView):
    """
    POST /skill/sessions/<id>/join/
    Returns a LiveKit token + the WS URL for the session room.
    Both the learner and the expert can call this; the server decides
    whether the caller can publish based on which side they are.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        user = request.user

        sess = SkillSession.objects.filter(id=session_id).select_related(
            "expert__teacher_profile__user", "learner_profile__account"
        ).first()
        if not sess:
            raise NotFound("Session not found.")

        # A session is only joinable once the expert has ACCEPTED it
        # (status 'confirmed'). A still-'requested' booking is pending and must
        # not be enterable by either side until the expert accepts.
        if sess.status != SkillSession.STATUS_CONFIRMED:
            raise PermissionDenied(
                f"Session is not joinable yet (status: {sess.status}). "
                "It must be accepted by the expert first."
            )

        # Decide identity + publish rights.
        is_expert   = (sess.expert.teacher_profile.user_id == user.id)
        is_learner  = (sess.learner_profile.account_id == user.id)

        if not is_expert and not is_learner:
            raise PermissionDenied("You are not a participant in this session.")

        room_name   = f"skill-session-{sess.id.hex}"
        identity    = f"expert-{user.id}" if is_expert else f"learner-{user.id}"
        can_publish = True   # both sides can publish their camera/mic

        token = _make_token(identity, room_name, can_publish=can_publish)

        # The expert entering the room IS "starting the class". Stamp it once so
        # the learner's dashboard can show a live "Join now" prompt immediately,
        # regardless of the originally scheduled time.
        if is_expert and sess.started_at is None:
            sess.started_at = timezone.now()
            sess.save(update_fields=["started_at", "updated_at"])

        # settings defines LIVEKIT_URL (not LIVEKIT_WS_URL); fall back to the
        # old name then localhost so existing envs keep working.
        ws_url = (
            getattr(settings, "LIVEKIT_URL", None)
            or getattr(settings, "LIVEKIT_WS_URL", None)
            or "ws://localhost:7880"
        )

        return Response({
            "token":     token,
            "ws_url":    ws_url,
            "room":      room_name,
            "identity":  identity,
            "is_expert": is_expert,
        })


class MySessionsView(APIView):
    """GET /skill/my-sessions/ — learner's sessions."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")
        qs = SkillSession.objects.filter(
            learner_profile=learner
        ).select_related("expert").order_by("-created_at")
        return Response([_session_card(s) for s in qs])


class TeacherSessionsView(APIView):
    """GET /skill/teacher/sessions/ — expert's incoming sessions."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep = ExpertProfile.objects.filter(
            teacher_profile__user=request.user, is_listed=True
        ).first()
        if not ep:
            raise PermissionDenied("No listed expert profile.")
        st = request.query_params.get("status")
        qs = ep.sessions.select_related("learner_profile").order_by("-created_at")
        if st:
            qs = qs.filter(status=st)
        return Response([_session_card(s, teacher_view=True) for s in qs])


class TeacherConfirmSessionView(APIView):
    """POST /skill/teacher/sessions/<id>/confirm/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        sess = _teacher_session(request.user, session_id)
        if sess.status != SkillSession.STATUS_REQUESTED:
            raise ValidationError("Session is not in 'requested' state.")
        sess.status = SkillSession.STATUS_CONFIRMED
        # Set scheduled time if provided.
        scheduled_for = request.data.get("scheduled_for")
        if scheduled_for:
            sess.scheduled_for = scheduled_for
        sess.save(update_fields=["status","scheduled_for","updated_at"])
        push_skill_bell(sess, "confirmed")
        return Response({"detail": "Session confirmed.", "id": str(sess.id)})


class TeacherCompleteSessionView(APIView):
    """POST /skill/teacher/sessions/<id>/complete/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        sess = _teacher_session(request.user, session_id)
        if sess.status != SkillSession.STATUS_CONFIRMED:
            raise ValidationError("Session must be confirmed before completing.")
        sess.status = SkillSession.STATUS_COMPLETED
        sess.save(update_fields=["status","updated_at"])
        # Bump expert session counter.
        ep = sess.expert
        ep.sessions_count = SkillSession.objects.filter(
            expert=ep, status=SkillSession.STATUS_COMPLETED
        ).count()
        ep.save(update_fields=["sessions_count"])
        # Release the reserved weekly slot back to the expert's open grid so the
        # same recurring time can be booked again. (Decline does the same.)
        if sess.slot_key:
            from .teacher_views import free_slot
            free_slot(ep, sess.slot_key)
        push_skill_bell(sess, "completed")
        return Response({"detail": "Session marked complete."})


# ── helpers ──────────────────────────────────────────────────────────────────

def _teacher_session(user, session_id):
    ep = ExpertProfile.objects.filter(teacher_profile__user=user).first()
    if not ep:
        raise PermissionDenied("No expert profile.")
    sess = SkillSession.objects.filter(id=session_id, expert=ep).first()
    if not sess:
        raise NotFound("Session not found.")
    return sess


def _session_card(s, teacher_view=False):
    from django.utils import timezone
    if teacher_view:
        who_name = s.learner_profile.display_name or s.learner_profile.full_name or "Student"
        who_id   = str(s.learner_profile.id)
    else:
        who_name = s.expert.display_name()
        who_id   = str(s.expert.id)
    # A confirmed session is joinable. It is "live" once the expert has started
    # it (started_at set) — and still considered live until it's completed.
    is_confirmed = s.status == SkillSession.STATUS_CONFIRMED
    is_live = bool(is_confirmed and s.started_at)
    return {
        "id":            str(s.id),
        "status":        s.status,
        "contact_mode":  s.contact_mode,
        "scheduled_for": s.scheduled_for,
        "started_at":    s.started_at,
        "live":          is_live,
        "joinable":      is_confirmed,
        "duration_mins": s.duration_mins,
        "note":          s.note,
        "meeting_url":   s.meeting_url,
        "payment_status":s.payment_status,
        "amount":        s.amount,
        "created_at":    s.created_at,
        ("learner" if teacher_view else "expert"): {"id": who_id, "name": who_name},
    }


# ── Admin: platform-wide session monitor ─────────────────────────────────
from accounts.permissions import IsAdmin


class AdminSessionListView(APIView):
    """
    GET /skill/admin/sessions/?status=<status>  → all 1-on-1 sessions.

    A friendly, read-only replacement for the raw Django-admin SkillSession
    list: every booking across the platform with learner, expert, status, and
    schedule. Optional ?status= filter (requested/confirmed/completed/cancelled).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (SkillSession.objects
              .select_related("learner_profile", "expert", "expert__teacher_profile")
              .order_by("-created_at"))
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        qs = qs[:300]  # cap for safety

        rows = []
        for s in qs:
            learner = (s.learner_profile.display_name
                       or s.learner_profile.full_name or "Student")
            rows.append({
                "id":            str(s.id),
                "learner":       learner,
                "expert":        s.expert.display_name(),
                "expert_id":     str(s.expert.id),
                "status":        s.status,
                "scheduled_for": s.scheduled_for,
                "started_at":    s.started_at,
                "duration_mins": s.duration_mins,
                "created_at":    s.created_at,
            })

        # Small status summary so the admin page can show counts.
        from django.db.models import Count
        counts = {row["status"]: row["n"] for row in
                  SkillSession.objects.values("status").annotate(n=Count("id"))}
        return Response({"sessions": rows, "counts": counts})


class AdminUserSkillProfileView(APIView):
    """
    GET /skill/admin/users/<user_id>/skill-profile/

    Skill-dev context for a single user, shown on the admin user-detail page:
      - whether they are an approved expert (+ a small summary), and
      - their 1-on-1 sessions as a LEARNER.
    Kept separate from the accounts user serializer so the skills app stays
    decoupled from accounts.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, user_id):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(id=user_id).first()
        if not user:
            raise NotFound("User not found.")

        ep = ExpertProfile.objects.filter(teacher_profile__user=user).first()
        expert = None
        if ep:
            expert = {
                "id":         str(ep.id),
                "name":       ep.display_name(),
                "listed":     ep.is_listed,
                "advertised": ep.is_advertised(),
                "featured":   ep.is_featured,
                "rating":     float(ep.rating) if ep.rating is not None else None,
                "sessions":   ep.sessions_count,
                "reach":      ep.reach_count,
            }

        sessions = (SkillSession.objects
                    .filter(learner_profile__user=user)
                    .select_related("expert")
                    .order_by("-created_at")[:50])
        learner_sessions = [{
            "id":            str(s.id),
            "expert":        s.expert.display_name(),
            "status":        s.status,
            "scheduled_for": s.scheduled_for,
            "created_at":    s.created_at,
        } for s in sessions]

        return Response({
            "is_expert":        bool(ep),
            "expert":           expert,
            "learner_sessions": learner_sessions,
        })
