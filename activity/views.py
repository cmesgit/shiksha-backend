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
    longer silently clears a child's unread badge."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        audience, profile, err = _identity(request)
        if err:
            return err
        _scoped_qs(request.user, audience, profile).filter(
            is_read=False
        ).update(is_read=True)
        return Response({"status": "ok"})
