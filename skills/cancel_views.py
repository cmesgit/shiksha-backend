# PLACEMENT: backend/backend/skills/cancel_views.py   (NEW FILE — FILE 2 of 3)
#
# WHY: the student SkillSessionDetail page has a "Cancel this request" button
# that POSTs /skill/sessions/<id>/cancel/ — its own header comment admits the
# endpoint doesn't exist ("fails gracefully with an alert"). The model already
# has STATUS_CANCELLED, so this is the missing learner-side counterpart to the
# existing teacher decline. Cancellable states: requested / pending_payment
# (once the expert has CONFIRMED, cancellation goes through the expert —
# matching the teacher decline flow).

from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from accounts.auth_flow import get_active_profile
from .models import SkillSession
from .teacher_views import free_slot
from .notifications import push_skill_bell


CANCELLABLE = (SkillSession.STATUS_REQUESTED, SkillSession.STATUS_PENDING_PAYMENT)


class StudentCancelSessionView(APIView):
    """POST /api/skill/sessions/<session_id>/cancel/ — learner cancels their
    own not-yet-confirmed request."""

    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile first."},
                status=status.HTTP_403_FORBIDDEN,
            )

        session = SkillSession.objects.filter(
            id=session_id, learner_profile=learner
        ).first()
        if session is None:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if session.status == SkillSession.STATUS_CANCELLED:
            return Response({"status": session.status})  # idempotent

        if session.status not in CANCELLABLE:
            return Response(
                {"detail": (
                    "Only pending requests can be cancelled. For a confirmed "
                    "session, please message the expert to reschedule or cancel."
                ), "status": session.status},
                status=status.HTTP_409_CONFLICT,
            )

        session.status = SkillSession.STATUS_CANCELLED
        update_fields = ["status"]
        if hasattr(session, "cancelled_at"):
            session.cancelled_at = timezone.now()
            update_fields.append("cancelled_at")
        session.save(update_fields=update_fields)
        # Release the reserved weekly slot back to the expert's grid — it was
        # booked at request time, before the expert ever confirmed.
        if session.slot_key:
            free_slot(session.expert, session.slot_key)
        push_skill_bell(session, "cancelled")
        return Response({"status": session.status})
