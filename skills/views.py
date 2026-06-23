"""
PLACEMENT: backend/backend/skills/views.py
ACTION:    Replace the entire file.

Change from original:
  CreateOrderView.post() was storing the entire sessionDraft dict as a string
  via note=str(request.data.get("draft") or ""), producing raw Python repr
  like "{'topic': 'Intro to the skill', 'slot': None, ...}" in the session
  topic field.

  Fixed: _extract_note() extracts a clean "Topic: X. Requested slot: Y."
  sentence from the draft dict instead.
"""
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from accounts.models import LearnerProfile, Role, UserRole
from accounts.permissions import IsAdmin
from accounts.auth_flow import get_active_profile

from .models import (
    SkillCategory,
    ExpertProfile,
    TeacherApplication,
    InterviewSlot,
    Interview,
    Evaluation,
    SkillSession,
)
from .serializers import (
    SkillCategorySerializer,
    ExpertCardSerializer,
    TeacherApplicationCreateSerializer,
    InterviewSlotSerializer,
    ReviewQueueSerializer,
    EvaluationSerializer,
    SkillSessionSerializer,
)


# =====================================================
# HELPERS
# =====================================================

def _extract_note(draft):
    """
    Build a clean human-readable note from the sessionDraft object the
    frontend sends via POST /skill/payments/create-order/.

    draft shape: { topic, note, slotLabel, date, time, duration_mins, expertId, ... }

    Returns a plain string like "Topic: React hooks. Requested slot: Mon 23 · 6 PM."
    Falls back gracefully when fields are missing.
    """
    if not draft:
        return ""

    # Safeguard: if it somehow arrives as a string already, pass it through
    if isinstance(draft, str):
        return draft[:200]

    parts = []

    topic = (draft.get("topic") or "").strip()
    # Skip generic default topics that add no information
    generic = {"intro to the skill", "1-on-1 session with", "intro to"}
    if topic and not any(topic.lower().startswith(g) for g in generic):
        parts.append(f"Topic: {topic}.")

    slot_label = (draft.get("slotLabel") or "").strip()
    if slot_label and slot_label.lower() not in ("none", "null", ""):
        parts.append(f"Requested slot: {slot_label}.")
    elif draft.get("date") and draft.get("time"):
        parts.append(f"Requested: {draft['date']} · {draft['time']}.")

    duration = draft.get("duration_mins")
    if duration and int(duration) != 60:
        parts.append(f"Duration: {duration} min.")

    # If we got nothing useful, use the raw note field as fallback
    if not parts:
        raw = (draft.get("note") or "").strip()
        if raw:
            return raw[:200]
        if topic:
            return topic[:200]

    return " ".join(parts)


# =====================================================
# DIRECTORY (public)
# =====================================================

class CategoryListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = SkillCategory.objects.filter(is_active=True)
        return Response(SkillCategorySerializer(qs, many=True).data)


class ExpertListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            ExpertProfile.objects
            .filter(is_listed=True)
            .select_related("category", "teacher_profile__user")
        )
        cat = request.query_params.get("cat") or request.query_params.get("category")
        if cat:
            qs = qs.filter(category__slug=cat)

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(headline__icontains=search)

        return Response(ExpertCardSerializer(qs, many=True, context={"request": request}).data)


class ExpertDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, expert_id):
        expert = (
            ExpertProfile.objects
            .filter(id=expert_id, is_listed=True)
            .select_related("category", "teacher_profile__user")
            .first()
        )
        if not expert:
            raise NotFound("Expert not found.")
        return Response(ExpertCardSerializer(expert, context={"request": request}).data)


# =====================================================
# LEARNER REGISTRATION
# =====================================================

class StudentRegisterView(APIView):
    """
    Guest-student entry for the skill feature. Account creation itself goes
    through accounts signup; this just guarantees the logged-in account has
    at least one learner profile to book sessions with.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        profile = user.learner_profiles.filter(is_active=True).first()
        if not profile:
            profile = LearnerProfile.objects.create(
                account=user,
                display_name=user.username or "Learner",
                relationship=LearnerProfile.RELATIONSHIP_SELF,
                is_default=not user.learner_profiles.exists(),
            )
        return Response({"ok": True, "profile_id": str(profile.id)}, status=status.HTTP_201_CREATED)


# =====================================================
# TEACHER APPLICATION + SCREENING
# =====================================================

class TeacherApplicationCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TeacherApplicationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = serializer.save(
            user=request.user,
            status=TeacherApplication.STATUS_SUBMITTED,
        )
        return Response(
            {"ok": True, "applicationId": str(application.id), "status": application.status},
            status=status.HTTP_201_CREATED,
        )


class InterviewSlotListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = [s for s in InterviewSlot.objects.filter(is_active=True) if s.is_open]
        return Response(InterviewSlotSerializer(qs, many=True).data)


class ScheduleInterviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, application_id):
        application = (
            TeacherApplication.objects
            .filter(id=application_id, user=request.user)
            .first()
        )
        if not application:
            raise NotFound("Application not found.")

        slot_id = request.data.get("slot")
        slot = None
        scheduled_for = None
        if slot_id:
            slot = InterviewSlot.objects.filter(id=slot_id, is_active=True).first()
            if not slot or not slot.is_open:
                raise ValidationError({"slot": "That slot is no longer available."})
            scheduled_for = slot.starts_at
        else:
            scheduled_for = request.data.get("scheduled_for")
            if not scheduled_for:
                raise ValidationError("A slot or scheduled_for is required.")

        with transaction.atomic():
            interview, _ = Interview.objects.update_or_create(
                application=application,
                defaults={"slot": slot, "scheduled_for": scheduled_for},
            )
            if slot:
                slot.booked_count += 1
                slot.save(update_fields=["booked_count"])
            application.status = TeacherApplication.STATUS_INTERVIEW_SCHEDULED
            application.save(update_fields=["status", "updated_at"])

        return Response({"ok": True, "scheduled_for": interview.scheduled_for})


# =====================================================
# ADMIN: reviewer queue + evaluation
# =====================================================

class ReviewQueueView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            TeacherApplication.objects
            .exclude(status=TeacherApplication.STATUS_REJECTED)
            .select_related("category", "user", "interview")
            .order_by("-created_at")
        )
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return Response(ReviewQueueSerializer(qs, many=True, context={"request": request}).data)


class SubmitEvaluationView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, application_id):
        application = (
            TeacherApplication.objects
            .select_related("user")
            .filter(id=application_id)
            .first()
        )
        if not application:
            raise NotFound("Application not found.")

        interview = getattr(application, "interview", None)
        if not interview:
            interview = Interview.objects.create(
                application=application, scheduled_for=timezone.now(),
                status=Interview.STATUS_COMPLETED,
            )

        serializer = EvaluationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        evaluation = serializer.save(interview=interview, evaluator=request.user)

        decision = evaluation.decision
        if decision == Evaluation.DECISION_APPROVE:
            application.status = TeacherApplication.STATUS_APPROVED
            self._approve_expert(application, evaluation)
        elif decision == Evaluation.DECISION_HOLD:
            application.status = TeacherApplication.STATUS_HOLD
        else:
            application.status = TeacherApplication.STATUS_REJECTED

        application.reviewed_by = request.user
        application.decided_at = timezone.now()
        application.save(update_fields=["status", "reviewed_by", "decided_at", "updated_at"])
        interview.status = Interview.STATUS_COMPLETED
        interview.save(update_fields=["status"])

        return Response({"ok": True, "status": application.status})

    def _approve_expert(self, application, evaluation):
        user = application.user
        tp = getattr(user, "teacher_profile", None)
        if tp is None:
            raise ValidationError(
                "Applicant has no TeacherProfile; complete teacher onboarding first."
            )

        tp.is_approved = True
        if evaluation.recommended_tier:
            tp.tier = evaluation.recommended_tier
        TP = tp.__class__
        if tp.teacher_type == TP.TYPE_FACULTY:
            tp.teacher_type = TP.TYPE_BOTH
        elif not tp.teacher_type:
            tp.teacher_type = TP.TYPE_GUEST
        tp.save(update_fields=["is_approved", "tier", "teacher_type"])

        rate_band = {
            Evaluation.TIER_STANDARD: 40000,
            Evaluation.TIER_SENIOR:   50000,
            Evaluation.TIER_EXPERT:   60000,
        }
        ExpertProfile.objects.update_or_create(
            teacher_profile=tp,
            defaults={
                "category":     application.category,
                "headline":     application.headline or application.skill_name,
                "skill_tags":   application.skill_tags or [],
                "bio":          tp.bio or application.method_note,
                "hourly_rate":  rate_band.get(evaluation.recommended_tier, 35000),
                "is_listed":    True,
            },
        )

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        ur, _ = UserRole.objects.get_or_create(user=user, role=teacher_role)
        if not ur.is_active:
            ur.approve(self.request.user)


# =====================================================
# SESSIONS + PAYMENT
# =====================================================

class SessionRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        learner = get_active_profile(request)
        if learner is None:
            raise PermissionDenied("Select a learner profile before requesting a session.")

        serializer = SkillSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = serializer.save(
            learner_profile=learner,
            status=SkillSession.STATUS_REQUESTED,
        )
        return Response(
            {"ok": True, "sessionId": str(session.id)},
            status=status.HTTP_201_CREATED,
        )


class CreateOrderView(APIView):
    """
    Book a session — currently FREE.

    Payment is intentionally disabled for now: instead of creating a Razorpay
    order and parking the session in `pending_payment`, we confirm the session
    immediately and mark it settled (nothing owed). The endpoint name and
    response shape are unchanged so the existing frontend keeps working.

    To re-enable paid sessions later, restore the Razorpay order-create here,
    set status=STATUS_PENDING_PAYMENT / payment_status=PAYMENT_UNPAID, and add a
    server-side /skill/payments/verify/ endpoint that confirms the signature
    before flipping the session to confirmed/paid.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        learner = get_active_profile(request)
        if learner is None:
            raise PermissionDenied("Select a learner profile first.")

        expert_id = request.data.get("teacherId") or request.data.get("expert")
        if not expert_id:
            raise ValidationError("expert/teacherId is required.")

        expert = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not expert:
            raise NotFound("Expert not found.")

        session = SkillSession.objects.create(
            learner_profile=learner,
            expert=expert,
            contact_mode=SkillSession.CONTACT_SESSION,
            status=SkillSession.STATUS_CONFIRMED,
            payment_status=SkillSession.PAYMENT_PAID,
            amount=0,
            # FIXED: was str(draft) which stored raw Python repr dict string.
            # Now extracts a clean "Topic: X. Requested slot: Y." sentence.
            note=_extract_note(request.data.get("draft")),
        )

        booking_ref = f"SHK-{session.id.hex[:8].upper()}"

        return Response({
            "ok":        True,
            "bookingId": booking_ref,
            "sessionId": str(session.id),
            "amount":    0,
            "free":      True,
        }, status=status.HTTP_201_CREATED)
