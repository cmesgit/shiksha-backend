# PLACEMENT: backend/backend/notifications/views.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/views.py
#
# Two API surfaces over the SAME table:
#
#   Canonical (new)   /api/notifications/...        → NotificationSerializer
#   Legacy alias      /api/forum/notifications/...  → LegacyForumNotificationSerializer
#
# The legacy views exist so the three deployed dashboards keep working
# unchanged while you build the redesigned forum against the canonical
# endpoints. When every bell is migrated, delete the Legacy* views here
# and the three notification paths in forum/urls.py.

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import tracks as _tracks
from .models import Notification
from .serializers import (
    LegacyForumNotificationSerializer,
    NotificationSerializer,
)


def _int_param(request, name, default, maximum):
    """Parse ?name= as a positive int; garbage falls back to the default."""
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(1, value), maximum)


def _base_qs(user):
    return (
        Notification.objects.filter(recipient=user)
        .select_related("actor")
    )


def _apply_audience_filters(qs, request):
    """Apply the three audience scopes shared by the list and count views.

    All three follow the same `__in=["", value]` shape: asking for a scope
    returns rows in that scope PLUS the unscoped rows, so narrowing a bell
    never hides a genuinely account-wide / cross-track notification. Kept
    in one place because the list and the badge MUST agree — a badge
    counting rows the list won't show is the classic phantom-unread bug.
    """
    role = request.query_params.get("role")
    if role:
        qs = qs.filter(audience_role__in=["", role.upper()])

    # M2 (Phase 3 §18): precise per-identity filter. A dashboard sends
    # its identity key ("L:<uuid>" / "T:<id>") and gets rows scoped to
    # that identity PLUS account-wide rows (blank audience_identity).
    # This is what actually keeps child A's bell from showing child B's
    # notifications; `role` alone can't, since both children are STUDENT.
    identity = request.query_params.get("identity")
    if identity:
        qs = qs.filter(audience_identity__in=["", identity])

    # Track scope. Orthogonal to identity — see notifications/tracks.py for
    # why one cannot be derived from the other.
    return _tracks.filter_queryset(qs, request.query_params.get("track"))


def _track_unread_counts(user):
    """EXACT unread counts per track — {"academy": n, "skill": n, "general": n}.

    Exact, not scope-style: "general" is the neutral rows only, and is NOT
    added into the other two. The cross-track peek ("2 new in Skill Dev")
    needs the count of rows the current bell is genuinely NOT showing.
    """
    rows = (
        Notification.objects
        .filter(recipient=user, is_read=False)
        .values_list("track")
        .annotate(n=Count("track"))
    )
    counts = {track or "general": n for track, n in rows}
    return {
        "academy": counts.get(_tracks.ACADEMY, 0),
        "skill": counts.get(_tracks.SKILL, 0),
        "general": counts.get("general", 0),
    }


# =====================================================
# Canonical endpoints — /api/notifications/
# =====================================================

class ListNotificationsView(APIView):
    """GET /api/notifications/

    Query params:
      page, page_size (≤100)
      unread=1            → only unread rows
      verb_prefix=forum.  → one product area only
      role=STUDENT        → rows for that dashboard ROLE + unscoped rows
                            (coarse; both children are STUDENT)
      identity=L:<uuid>   → rows for that ONE identity + account-wide rows
                            (precise per-profile scope — send this from each
                            dashboard so child A's bell never shows child B's)
      track=academy|skill → rows for that track + cross-track rows. Send it
                            from each bell so Skill Dev bookings never render
                            inside Academy chrome. Cross-track rows (chat,
                            forum, counselling) are never hidden by this.

    The response also carries `track_unread`: EXACT per-track unread counts
    (not "+ neutral"), so a bell can render a "2 new in Skill Dev" peek
    without a second request. Exact counts are what that affordance needs —
    folding neutral into both numbers would make the peek claim rows the
    user can already see in the bell they're looking at.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _base_qs(request.user)

        if request.query_params.get("unread") in ("1", "true"):
            qs = qs.filter(is_read=False)

        verb_prefix = request.query_params.get("verb_prefix")
        if verb_prefix:
            qs = qs.filter(verb__startswith=verb_prefix)

        qs = _apply_audience_filters(qs, request)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 8, 100)
        total = qs.count()
        # The badge counts the SAME scope the list renders. This previously
        # counted the whole account regardless of ?role=/?identity=, so a
        # scoped bell showed a count it could not account for; with ?track=
        # that discrepancy would have been permanent and visible.
        unread = _apply_audience_filters(
            _base_qs(request.user), request).filter(is_read=False).count()
        start = (page - 1) * page_size

        serializer = NotificationSerializer(
            qs[start:start + page_size], many=True)
        return Response({
            "results": serializer.data,
            "count": total,
            "unread_count": unread,
            "track_unread": _track_unread_counts(request.user),
        })


class UnreadCountView(APIView):
    """GET /api/notifications/unread-count/ — cheap badge poll.

    Honors the same ?identity= / ?role= / ?track= scoping as the list
    endpoint, so a per-profile, per-track bell shows a matching count.
    Sending none of them preserves the old account-wide count exactly.

    Also returns `track_unread` (exact per-track counts) so the bell can
    render its cross-track peek from this cheap poll alone.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user, is_read=False)
        qs = _apply_audience_filters(qs, request)
        return Response({
            "unread_count": qs.count(),
            "track_unread": _track_unread_counts(request.user),
        })


class MarkAllNotificationsReadView(APIView):
    """POST /api/notifications/read/

    Honors ?role= / ?identity= / ?track= (also accepted in the JSON body,
    since this is a POST and callers naturally put them there). Without
    them it clears the whole account, exactly as before.

    Scoping matters here: "mark all read" in the Academy bell used to clear
    the Skill Dev bell too, which silently destroys the cross-track peek —
    the user dismisses notifications they were never shown.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        qs = Notification.objects.filter(recipient=request.user, is_read=False)
        qs = _apply_audience_filters(qs, _BodyOrQuery(request))
        updated = qs.update(is_read=True)
        return Response({
            "detail": "All notifications marked as read.",
            "updated": updated,
            "track_unread": _track_unread_counts(request.user),
        })


class _BodyOrQuery:
    """Adapter so _apply_audience_filters can read a POST's scope params
    from either the query string or the JSON body without duplicating the
    filter logic. Query string wins when both are present."""

    def __init__(self, request):
        merged = {}
        if isinstance(getattr(request, "data", None), dict):
            merged.update(request.data)
        merged.update(request.query_params.dict())
        self.query_params = merged


class MarkNotificationReadView(APIView):
    """POST /api/notifications/<id>/read/"""

    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        notification = get_object_or_404(
            Notification, pk=notification_id, recipient=request.user
        )
        notification.is_read = True
        notification.save(update_fields=["is_read"])
        return Response({"detail": "Notification marked as read."})


# =====================================================
# Legacy aliases — mounted at /api/forum/notifications/
# =====================================================

class LegacyListNotificationsView(APIView):
    """Old GET /api/forum/notifications/ — identical response shape.

    Not filtered to forum verbs on purpose: this endpoint has always been
    the whole bell for its user, and until the frontends migrate, forum
    verbs are the only persisted verbs anyway. Filtering here would hide
    counseling/assignment rows from users still on the old bell later.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _base_qs(request.user)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 8, 100)
        total = qs.count()
        start = (page - 1) * page_size

        serializer = LegacyForumNotificationSerializer(
            qs[start:start + page_size], many=True)
        return Response({
            "results": serializer.data,
            "count": total,
        })


# Same semantics as before; reuse the canonical implementations.
LegacyMarkAllNotificationsReadView = MarkAllNotificationsReadView
LegacyMarkNotificationReadView = MarkNotificationReadView


# =====================================================
# Channel preferences — /api/notifications/preferences/
# =====================================================

class PreferencesView(APIView):
    """GET  → current switches + the category vocabulary for the UI.
    PUT  → partial update ({"sms_enabled": false} alone is valid).

    These gate OPT_OUT-level sends only; REQUIRED (transactional)
    messages — booking confirmations, cancellations, receipts — are
    always delivered regardless (see notifications/policy.py)."""

    permission_classes = [IsAuthenticated]

    def _payload(self, prefs):
        from . import policy
        from .models import NotificationPreference
        return {
            "email_enabled": prefs.email_enabled,
            "sms_enabled": prefs.sms_enabled,
            "push_enabled": prefs.push_enabled,
            "muted_categories": prefs.muted_categories or [],
            "categories": policy.CATEGORIES,
            "language": prefs.language,
            # Sent so Settings → Notifications renders the language picker from
            # the server's list instead of a hardcoded copy that can drift.
            "languages": [
                {"value": v, "label": l}
                for v, l in NotificationPreference.LANGUAGE_CHOICES
            ],
        }

    def get(self, request):
        from .models import NotificationPreference
        return Response(self._payload(
            NotificationPreference.for_user(request.user)))

    def put(self, request):
        from . import policy
        from .models import NotificationPreference

        prefs = NotificationPreference.for_user(request.user)
        data = request.data or {}

        for field in ("email_enabled", "sms_enabled", "push_enabled"):
            if field in data:
                if not isinstance(data[field], bool):
                    return Response({"detail": f"{field} must be a boolean."},
                                    status=400)
                setattr(prefs, field, data[field])

        if "muted_categories" in data:
            muted = data["muted_categories"]
            if (not isinstance(muted, list)
                    or any(c not in policy.CATEGORIES for c in muted)):
                return Response(
                    {"detail": "muted_categories must be a list drawn from "
                               f"{policy.CATEGORIES}."},
                    status=400)
            prefs.muted_categories = muted

        if "language" in data:
            valid = {v for v, _ in NotificationPreference.LANGUAGE_CHOICES}
            if data["language"] not in valid:
                return Response(
                    {"detail": f"language must be one of {sorted(valid)}."},
                    status=400)
            prefs.language = data["language"]

        prefs.save()
        return Response(self._payload(prefs))
