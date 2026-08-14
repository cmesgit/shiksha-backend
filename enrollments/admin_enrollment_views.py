"""
admin_enrollment_views.py — admin management of academic course enrollments.

Powers the admin app's "Enrollment Management" page:
  GET  /api/enrollments/admin/enrollments/?status=<ACTIVE|REVOKED>&q=<text>
       → { "results": [ {id,user_name,user_email,course_title,batch_code,status,enrolled_at,
                          subscription_expires_at}, ... ] }
  POST /api/enrollments/admin/enrollments/<uuid>/action/
       { "action": "revoke" | "reactivate" }
       { "action": "move_batch", "batch": "<uuid|null>" }

Reuses BatchStudentSerializer (same row shape as the batch roster). The action
toggles Enrollment.status between ACTIVE and REVOKED, or moves the enrollment
to a different batch of the same course.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, ValidationError
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery

from accounts.permissions import IsAdmin
from courses.models import Batch
from .models import Enrollment, Subscription
from .serializers import BatchStudentSerializer


def _subscription_expiry_subquery():
    """Latest subscription's expires_at for the enrollment's (course, student),
    dual-keyed like the rest of this codebase (courses/progress_stats.py's
    _dual_key_q): match on learner_profile when the enrollment has one,
    otherwise fall back to a legacy NULL-profile subscription for the same
    account. Feeds the admin list's computed "Expired" display state without
    a new stored Enrollment status."""
    matching = Subscription.objects.filter(course_id=OuterRef("course_id")).filter(
        Q(learner_profile_id=OuterRef("learner_profile_id"), learner_profile_id__isnull=False)
        | Q(learner_profile_id__isnull=True, user_id=OuterRef("user_id"))
    ).order_by("-expires_at")
    return Subquery(matching.values("expires_at")[:1])


class AdminEnrollmentListView(APIView):
    """All enrollments, filterable by status + free-text (email / course / name)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (Enrollment.objects
              .select_related("user", "learner_profile", "course")
              .annotate(subscription_expires_at=_subscription_expiry_subquery())
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
    """Revoke (ACTIVE→REVOKED), reactivate (REVOKED→ACTIVE), or move one
    enrollment to a different batch of the same course."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, enrollment_id):
        e = (Enrollment.objects
             .select_related("user", "learner_profile", "course", "batch")
             .filter(id=enrollment_id).first())
        if not e:
            raise NotFound("Enrollment not found.")

        action = (request.data.get("action") or "").lower()
        if action == "revoke":
            e.status = Enrollment.STATUS_REVOKED
            e.save(update_fields=["status"])
        elif action == "reactivate":
            e.status = Enrollment.STATUS_ACTIVE
            e.save(update_fields=["status"])
        elif action == "move_batch":
            self._move_batch(e, request.data.get("batch"))
        else:
            raise ValidationError(
                "action must be 'revoke', 'reactivate' or 'move_batch'."
            )

        row = BatchStudentSerializer(e, context={"request": request}).data
        return Response({"ok": True, **row})

    def _move_batch(self, enrollment, batch_id):
        """Reassign the enrollment's batch. Batch-scoped content (assignments,
        materials, recordings, per-batch chapter coverage) is resolved from this
        FK, so moving a student changes what they see — intended, but it is why
        the target batch must belong to the same course."""
        if batch_id in (None, "", "null"):
            # Detach → course-wide. Legitimate (matches the NULL-batch legacy
            # rows), so it is allowed rather than treated as a bad request.
            enrollment.batch = None
            enrollment.save(update_fields=["batch"])
            return

        batch = Batch.objects.filter(id=batch_id).select_related("course").first()
        if not batch:
            raise ValidationError({"batch": "Batch not found."})
        if batch.course_id != enrollment.course_id:
            raise ValidationError({
                "batch": "That batch belongs to a different course.",
            })
        if batch.id == enrollment.batch_id:
            return  # no-op, already there
        if not batch.is_active:
            raise ValidationError({"batch": "That batch is inactive."})
        if batch.is_full:
            raise ValidationError({
                "batch": f"'{batch.name}' is full ({batch.seats_taken}/{batch.capacity}).",
            })

        enrollment.batch = batch
        # Keep the legacy free-text code in step with the FK; dashboards and the
        # roster still read batch_code for pre-Batch-model rows.
        enrollment.batch_code = batch.code
        enrollment.save(update_fields=["batch", "batch_code"])


class AdminEnrollmentBulkBatchView(APIView):
    """Place many enrollments into one batch at once.

        POST /api/enrollments/admin/enrollments/bulk-batch/
             { "enrollment_ids": [...], "batch": "<uuid|null>" }

    Exists because placing a new cohort is inherently a bulk job — doing it one
    student at a time is the difference between one action and forty.

    Capacity is checked against the WHOLE selection, not per row: filling the
    last seat mid-loop would otherwise let a batch overflow depending on
    ordering. Nothing is written unless every row fits.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        ids = request.data.get("enrollment_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"enrollment_ids": "Provide a non-empty list."})
        if len(ids) > 500:
            raise ValidationError({"enrollment_ids": "At most 500 at a time."})

        enrollments = list(
            Enrollment.objects
            .select_related("course", "batch")
            .filter(id__in=ids)
        )
        missing = set(str(i) for i in ids) - {str(e.id) for e in enrollments}
        if missing:
            raise ValidationError({
                "enrollment_ids": f"{len(missing)} enrollment(s) not found.",
            })

        batch_id = request.data.get("batch")

        # Detach-to-course-wide needs no capacity/course checks.
        if batch_id in (None, "", "null"):
            with transaction.atomic():
                for e in enrollments:
                    e.batch = None
                    e.save(update_fields=["batch"])
            return Response({"ok": True, "updated": len(enrollments), "batch": None})

        batch = Batch.objects.filter(id=batch_id).select_related("course").first()
        if not batch:
            raise ValidationError({"batch": "Batch not found."})
        if not batch.is_active:
            raise ValidationError({"batch": "That batch is inactive."})

        wrong_course = [e for e in enrollments if e.course_id != batch.course_id]
        if wrong_course:
            raise ValidationError({
                "batch": (
                    f"{len(wrong_course)} of the selected enrollments are for a "
                    f"different course than '{batch.name}'. A batch only holds "
                    f"students of its own course."
                ),
            })

        # Only rows actually moving IN consume new seats; ones already in this
        # batch are no-ops and must not be double-counted against capacity.
        incoming = [e for e in enrollments if e.batch_id != batch.id]
        if batch.capacity is not None:
            free = batch.capacity - batch.seats_taken
            if len(incoming) > free:
                raise ValidationError({
                    "batch": (
                        f"'{batch.name}' has {max(0, free)} seat(s) free but "
                        f"{len(incoming)} student(s) would move in."
                    ),
                })

        with transaction.atomic():
            for e in incoming:
                e.batch = batch
                e.batch_code = batch.code
                e.save(update_fields=["batch", "batch_code"])

        return Response({
            "ok": True,
            "updated": len(incoming),
            "skipped_already_in_batch": len(enrollments) - len(incoming),
            "batch": {"id": str(batch.id), "name": batch.name, "code": batch.code},
        })
