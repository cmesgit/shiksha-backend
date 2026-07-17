# skills/reschedule_views.py
#
# Teacher-proposes / learner-responds reschedule flow for SkillSession,
# mirroring sessions_app.views's reschedule_request/confirm_reschedule/
# decline_reschedule for PrivateSession — adapted to SkillSession's shape,
# which books a slot_key against the expert's weekly grid rather than
# separate date/time fields.
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from accounts.auth_flow import get_active_profile
from .models import SkillSession
from .teacher_views import _get_expert, slot_is_open, mark_slot_booked, free_slot
from .views import _slot_to_datetime
from .notifications import push_skill_bell

RESCHEDULABLE = (SkillSession.STATUS_REQUESTED, SkillSession.STATUS_CONFIRMED)


class TeacherRescheduleSessionView(APIView):
    """POST /skill/teacher/sessions/<session_id>/reschedule/ — expert proposes
    a new slot; the session waits on the learner's confirmation."""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        ep = _get_expert(request.user)
        sess = SkillSession.objects.filter(id=session_id, expert=ep).first()
        if not sess:
            raise NotFound("Session not found.")
        if sess.status not in RESCHEDULABLE:
            raise ValidationError(
                f"Cannot reschedule a session with status '{sess.status}'."
            )
        if sess.status == SkillSession.STATUS_CONFIRMED and sess.started_at:
            raise ValidationError(
                "This session is already live — it can't be rescheduled."
            )

        new_slot_key = (request.data.get("slot_key") or "").strip()
        if not new_slot_key:
            raise ValidationError("slot_key is required.")
        if not slot_is_open(ep, new_slot_key):
            raise ValidationError("That slot isn't open on your availability grid.")

        sess.proposed_slot_key = new_slot_key
        sess.proposed_scheduled_for = _slot_to_datetime(new_slot_key)
        sess.reschedule_reason = request.data.get("reason", "")
        sess.status = SkillSession.STATUS_NEEDS_RECONFIRMATION
        sess.save(update_fields=[
            "proposed_slot_key", "proposed_scheduled_for",
            "reschedule_reason", "status", "updated_at",
        ])
        push_skill_bell(sess, "reschedule_proposed")
        return Response({"ok": True, "status": sess.status})


class StudentConfirmRescheduleView(APIView):
    """POST /skill/sessions/<session_id>/confirm-reschedule/ — learner accepts
    the expert's proposed slot."""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        if learner is None:
            raise ValidationError("Select a learner profile first.")
        sess = SkillSession.objects.filter(id=session_id, learner_profile=learner).first()
        if not sess:
            raise NotFound("Session not found.")
        if sess.status != SkillSession.STATUS_NEEDS_RECONFIRMATION:
            raise ValidationError("This session isn't awaiting reconfirmation.")

        old_slot_key = sess.slot_key
        if old_slot_key and old_slot_key != sess.proposed_slot_key:
            free_slot(sess.expert, old_slot_key)
        if sess.proposed_slot_key:
            mark_slot_booked(sess.expert, sess.proposed_slot_key)

        sess.slot_key = sess.proposed_slot_key
        sess.scheduled_for = sess.proposed_scheduled_for
        sess.proposed_slot_key = ""
        sess.proposed_scheduled_for = None
        sess.status = SkillSession.STATUS_CONFIRMED
        sess.save(update_fields=[
            "slot_key", "scheduled_for", "proposed_slot_key",
            "proposed_scheduled_for", "status", "updated_at",
        ])
        push_skill_bell(sess, "reschedule_confirmed")
        return Response({"ok": True, "status": sess.status})


class StudentDeclineRescheduleView(APIView):
    """POST /skill/sessions/<session_id>/decline-reschedule/ — learner
    declines the expert's proposed slot, ending the session."""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        if learner is None:
            raise ValidationError("Select a learner profile first.")
        sess = SkillSession.objects.filter(id=session_id, learner_profile=learner).first()
        if not sess:
            raise NotFound("Session not found.")
        if sess.status != SkillSession.STATUS_NEEDS_RECONFIRMATION:
            raise ValidationError("This session isn't awaiting reconfirmation.")

        if sess.slot_key:
            free_slot(sess.expert, sess.slot_key)
        sess.status = SkillSession.STATUS_CANCELLED
        sess.save(update_fields=["status", "updated_at"])
        push_skill_bell(sess, "reschedule_declined")
        return Response({"ok": True, "status": sess.status})
