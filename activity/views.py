"""
activity/views.py  ·  FULL REPLACEMENT — context + profile scoped feed
──────────────────────────────────────────────────────────────────────
The feed and its mark-read endpoints previously filtered on user only,
so on a one-email account:
  • Profile A's bell showed Profile B's assignments,
  • the teacher bell filled with learner rows (and vice versa),
  • "mark all read" in ANY tab wiped the unread state of every
    identity on the account.

Every endpoint now resolves the caller's identity from the same JWT
claims the auth flow issues (context + active_profile) and scopes to
it. Selection rules mirror dashboard/views.py exactly:

  learner  →  audience=LEARNER  AND (learner_profile = active OR NULL)
  teacher  →  audience=TEACHER  AND learner_profile IS NULL
  account  →  409 profile_required (nothing to show before a pick)

NULL-profile learner rows are the pre-migration backlog — visible to
all profiles of the account until they age out (no data loss).
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth_flow import get_active_profile, CTX_LEARNER, CTX_TEACHER

from .models import Activity
from .serializers import ActivitySerializer


def _identity(request):
    """→ (audience, profile, error_response|None) for the caller."""
    token = getattr(request, "auth", None)
    context = token.get("context") if token else None

    if context == CTX_TEACHER:
        return Activity.AUDIENCE_TEACHER, None, None

    if context == CTX_LEARNER:
        profile = get_active_profile(request)
        if profile is None:
            return None, None, Response(
                {"code": "profile_required", "detail": "Select a learner profile."},
                status=status.HTTP_409_CONFLICT,
            )
        return Activity.AUDIENCE_LEARNER, profile, None

    return None, None, Response(
        {"code": "profile_required",
         "detail": "Pick a learner profile or enter teacher mode first."},
        status=status.HTTP_409_CONFLICT,
    )


def _skill_session_ct_id():
    """ContentType id of skills.SkillSession, or None if unavailable.

    Cached on the ContentType manager's own per-process cache, so this is
    one query per process rather than per request. Returns None rather than
    raising when the skills app isn't installed/migrated — the caller
    degrades to "nothing is a skill row".
    """
    try:
        from django.contrib.contenttypes.models import ContentType
        return ContentType.objects.get_for_model(
            __import__("skills.models", fromlist=["SkillSession"]).SkillSession
        ).id
    except Exception:
        return None


def _cross_track_unread(scoped_qs, track):
    """Unread rows in the track the caller is NOT currently viewing.

    Feeds the bell's cross-track peek. Returns 0 when no track filter is
    active, because then nothing is being hidden and a peek would be a lie.
    """
    if track not in ("academy", "skill"):
        return 0
    skill_ct = _skill_session_ct_id()
    if skill_ct is None:
        return 0
    unread = scoped_qs.filter(is_read=False)
    return (unread.exclude(content_type_id=skill_ct).count() if track == "skill"
            else unread.filter(content_type_id=skill_ct).count())


def _scoped_qs(user, audience, profile):
    qs = Activity.objects.filter(user=user, audience=audience)
    if audience == Activity.AUDIENCE_LEARNER:
        qs = qs.filter(Q(learner_profile=profile) | Q(learner_profile__isnull=True))
    else:
        qs = qs.filter(learner_profile__isnull=True)
    return qs


class ActivityFeedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        audience, profile, err = _identity(request)
        if err:
            return err

        now = timezone.now()
        qs = (
            _scoped_qs(request.user, audience, profile)
            .exclude(
                type__in=[
                    Activity.TYPE_SESSION,
                    Activity.TYPE_QUIZ,
                    Activity.TYPE_ASSIGNMENT,
                ],
                due_date__lt=now,
            )
            .order_by("-created_at")
        )

        activity_type = request.query_params.get("type")
        if activity_type:
            # Accept both vocabularies: canonical UPPERCASE and the
            # mobile-mapped lowercase (?type=material → ASSIGNMENT|SUBMISSION).
            t = activity_type.strip()
            lower_map = {
                "session":  [Activity.TYPE_SESSION],
                "quiz":     [Activity.TYPE_QUIZ],
                "material": [Activity.TYPE_ASSIGNMENT, Activity.TYPE_SUBMISSION],
            }
            qs = qs.filter(type__in=lower_map.get(t.lower(), [t.upper()]))

        # ── Track scope (Academy vs Skill Dev) ──────────────────────────
        # A skill row is one whose generic FK points at skills.SkillSession;
        # everything else is academy. Filtering on the ContentType id keeps
        # this a single indexed comparison instead of pulling every row and
        # probing content_type per object (which is what the serializer's
        # is_skill_session does, one row at a time).
        #
        # An unknown/absent ?track= is a deliberate no-op: a typo in a
        # client must not blank the bell. Same rule as
        # notifications.tracks.filter_queryset.
        track = (request.query_params.get("track") or "").strip().lower()
        if track in ("academy", "skill"):
            skill_ct = _skill_session_ct_id()
            if skill_ct is None:
                # skills app/table absent (fresh install): nothing can be a
                # skill row, so "skill" is empty and "academy" is everything.
                qs = qs.none() if track == "skill" else qs
            elif track == "skill":
                qs = qs.filter(content_type_id=skill_ct)
            else:
                qs = qs.exclude(content_type_id=skill_ct)

        cross_track_unread = _cross_track_unread(
            _scoped_qs(request.user, audience, profile), track)

        try:
            limit = min(int(request.query_params.get("limit", 20)), 50)
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            offset = 0

        total = qs.count()
        serializer = ActivitySerializer(qs[offset: offset + limit], many=True)
        return Response({
            "results": serializer.data,
            "total": total,
            "limit": limit,
            "offset": offset,
            # Unread count in the track this response is NOT showing, so the
            # bell can render its "2 new in Skill Dev" peek without a second
            # request. 0 when no ?track= was sent (nothing is being hidden).
            "cross_track_unread": cross_track_unread,
        })


class MarkActivityReadView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        audience, profile, err = _identity(request)
        if err:
            return err
        updated = (
            _scoped_qs(request.user, audience, profile)
            .filter(pk=pk)
            .update(is_read=True)
        )
        if not updated:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "ok"})


class MarkAllReadView(APIView):
    """Marks THIS identity's rows read — switching to the teacher tab no
    longer silently clears a child's unread badge.

    Also honours ?track=academy|skill (or the same key in the POST body):
    without it, "mark all read" in the Academy bell also cleared the Skill
    Dev bell, dismissing rows the user was never shown. Sending no track
    keeps the old clear-everything behaviour.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        audience, profile, err = _identity(request)
        if err:
            return err

        qs = _scoped_qs(request.user, audience, profile).filter(is_read=False)

        raw = request.query_params.get("track")
        if raw is None and isinstance(getattr(request, "data", None), dict):
            raw = request.data.get("track")
        track = (raw or "").strip().lower()

        if track in ("academy", "skill"):
            skill_ct = _skill_session_ct_id()
            if skill_ct is not None:
                qs = (qs.filter(content_type_id=skill_ct) if track == "skill"
                      else qs.exclude(content_type_id=skill_ct))
            elif track == "skill":
                qs = qs.none()

        qs.update(is_read=True)
        return Response({"status": "ok"})
