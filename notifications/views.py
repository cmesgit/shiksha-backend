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

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

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
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _base_qs(request.user)

        if request.query_params.get("unread") in ("1", "true"):
            qs = qs.filter(is_read=False)

        verb_prefix = request.query_params.get("verb_prefix")
        if verb_prefix:
            qs = qs.filter(verb__startswith=verb_prefix)

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

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 8, 100)
        total = qs.count()
        unread = _base_qs(request.user).filter(is_read=False).count()
        start = (page - 1) * page_size

        serializer = NotificationSerializer(
            qs[start:start + page_size], many=True)
        return Response({
            "results": serializer.data,
            "count": total,
            "unread_count": unread,
        })


class UnreadCountView(APIView):
    """GET /api/notifications/unread-count/ — cheap badge poll.

    Honors the same ?identity= / ?role= scoping as the list endpoint, so a
    per-profile bell shows a per-profile count. Sending neither preserves
    the old account-wide count exactly.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user, is_read=False)

        role = request.query_params.get("role")
        if role:
            qs = qs.filter(audience_role__in=["", role.upper()])

        identity = request.query_params.get("identity")
        if identity:
            qs = qs.filter(audience_identity__in=["", identity])

        return Response({"unread_count": qs.count()})


class MarkAllNotificationsReadView(APIView):
    """POST /api/notifications/read/"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True)
        return Response({"detail": "All notifications marked as read."})


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
