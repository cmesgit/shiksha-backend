# PLACEMENT: three NEW backend files + the url/model edit lines in BACKEND_EDITS.md
# ═══════════════════════════════════════════════════════════════════════════
# FILE 1 of 3
# PLACEMENT: backend/backend/sessions_app/availability.py   (NEW FILE)
#
# WHY: the teacher dashboard has a working Availability page
# (/teacher/private-sessions/availability) whose service calls
# GET/POST /api/sessions/teacher/availability/ — but no backend existed.
# The service swallowed the 404s (`catch → {}`), so teachers "saved" their
# hours into the void. This is the smallest real persistence for it: one
# JSON blob per teacher, matching exactly what the frontend already sends.
# ═══════════════════════════════════════════════════════════════════════════
import uuid

from django.conf import settings
from django.db import models

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from accounts.models import Role


class TeacherAvailability(models.Model):
    """One row per teacher: the availability object the frontend edits,
    stored verbatim as JSON. Schema-free on purpose — the page owns the
    shape; the backend only persists it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    teacher = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="private_session_availability",
    )
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "sessions_app"

    def __str__(self):
        return f"Availability<{self.teacher_id}>"


class TeacherAvailabilityView(APIView):
    """GET  → the caller's saved availability object ({} if never saved).
    POST → replace it with the request body. Teachers only."""

    permission_classes = [IsAuthenticated]

    def _gate(self, request):
        if not request.user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers can manage availability."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def get(self, request):
        blocked = self._gate(request)
        if blocked is not None:
            return blocked
        row = TeacherAvailability.objects.filter(teacher=request.user).first()
        return Response(row.data if row else {})

    def post(self, request):
        blocked = self._gate(request)
        if blocked is not None:
            return blocked
        data = request.data if isinstance(request.data, dict) else {}
        row, _ = TeacherAvailability.objects.update_or_create(
            teacher=request.user, defaults={"data": data}
        )
        return Response({"success": True, "data": row.data})
