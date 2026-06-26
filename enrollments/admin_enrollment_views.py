"""
admin_enrollment_views.py — admin management of academic course enrollments.

Powers the admin app's "Enrollment Management" page:
  GET  /api/enrollments/admin/enrollments/?status=<ACTIVE|REVOKED>&q=<text>
       → { "results": [ {id,user_name,user_email,course_title,batch_code,status,enrolled_at}, ... ] }
  POST /api/enrollments/admin/enrollments/<uuid>/action/   { "action": "revoke" | "reactivate" }

Reuses BatchStudentSerializer (same row shape as the batch roster). The action
toggles Enrollment.status between ACTIVE and REVOKED.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, ValidationError
from django.db.models import Q

from accounts.permissions import IsAdmin
from .models import Enrollment
from .serializers import BatchStudentSerializer


class AdminEnrollmentListView(APIView):
    """All enrollments, filterable by status + free-text (email / course / name)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (Enrollment.objects
              .select_related("user", "learner_profile", "course")
              .order_by("-enrolled_at"))

        status_filter = (request.query_params.get("status") or "").strip().upper()
        if status_filter in (Enrollment.STATUS_ACTIVE, Enrollment.STATUS_REVOKED):
            qs = qs.filter(status=status_filter)

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(user__email__icontains=q) |
                Q(course__title__icontains=q) |
                Q(learner_profile__first_name__icontains=q) |
                Q(learner_profile__last_name__icontains=q) |
                Q(batch_code__icontains=q)
            )

        rows = BatchStudentSerializer(qs[:500], many=True, context={"request": request}).data
        return Response({"results": rows, "count": len(rows)})


class AdminEnrollmentActionView(APIView):
    """Revoke (ACTIVE→REVOKED) or reactivate (REVOKED→ACTIVE) one enrollment."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, enrollment_id):
        e = (Enrollment.objects
             .select_related("user", "learner_profile", "course")
             .filter(id=enrollment_id).first())
        if not e:
            raise NotFound("Enrollment not found.")

        action = (request.data.get("action") or "").lower()
        if action == "revoke":
            e.status = Enrollment.STATUS_REVOKED
        elif action == "reactivate":
            e.status = Enrollment.STATUS_ACTIVE
        else:
            raise ValidationError("action must be 'revoke' or 'reactivate'.")
        e.save(update_fields=["status"])

        row = BatchStudentSerializer(e, context={"request": request}).data
        return Response({"ok": True, **row})
