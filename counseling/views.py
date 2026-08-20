# PLACEMENT: backend/backend/counseling/views.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/views.py
#
# API surface, by audience:
#
# PUBLIC (landing site)
#   GET  /counseling/specializations/
#   GET  /counseling/counselors/?specialization=&language=&search=
#   GET  /counseling/counselors/<id>/
#   GET  /counseling/counselors/<id>/slots/?days=14
#
# STUDENT (any authenticated account; learner_profile_id lets a parent act
# for a dependent — ownership is always checked against request.user)
#   GET/PUT /counseling/intake/?learner_profile_id=
#   GET  /counseling/match/?learner_profile_id=
#   POST /counseling/appointments/create/
#   GET  /counseling/appointments/?status=&upcoming=1
#   POST /counseling/appointments/<id>/cancel/
#   GET/PUT  /counseling/appointments/<id>/assessment/
#   POST /counseling/appointments/<id>/assessment/submit/
#   GET  /counseling/reports/
#
# COUNSELOR (approved CounselorProfile on the account)
#   POST /counseling/counselor/apply/          (any authenticated user)
#   GET/PUT /counseling/counselor/me/
#   GET/POST /counseling/counselor/availability/
#   DELETE   /counseling/counselor/availability/<id>/
#   GET  /counseling/counselor/appointments/?status=&upcoming=1
#   POST /counseling/counselor/appointments/<id>/meeting-link/
#   POST /counseling/counselor/appointments/<id>/complete/
#   GET  /counseling/counselor/appointments/<id>/student/
#   GET/POST /counseling/counselor/appointments/<id>/notes/
#   GET/PUT  /counseling/counselor/appointments/<id>/report/   (?publish)
#
# ADMIN (is_staff — mirrors the teacher-approvals pattern)
#   GET  /counseling/admin/applications/?status=
#   POST /counseling/admin/applications/<id>/action/   approve · reject ·
#                                                      suspend · relist
#   GET  /counseling/admin/appointments/?status=&search=   (+ stats)
#
# Every event notifies through the site-wide notifications app
# (verbs "counseling.*"); bookings and published reports also email.

from django.db import transaction
from django.db.models import Avg, Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from notifications.services import notify

from . import services
from .models import (
    Appointment, AssessmentResponse, AssessmentTemplate, AvailabilitySlot,
    CounselingIntake, CounselorProfile, SessionNote, SessionReport,
    Specialization,
)
from .serializers import (
    AdminAppointmentSerializer, AdminCounselorApplicationSerializer,
    AppointmentSerializer, AssessmentSerializer, AvailabilitySlotSerializer,
    CounselorCardSerializer, CounselorDetailSerializer,
    CounselorSelfSerializer, CreateAppointmentSerializer, IntakeSerializer,
    MatchedCounselorSerializer, SessionNoteSerializer,
    SessionReportSerializer, SpecializationSerializer,
)


# =====================================================
# Helpers & permissions
# =====================================================

class IsApprovedCounselor(BasePermission):
    """Authenticated + an APPROVED CounselorProfile on this account."""

    message = "This endpoint is for approved counselors."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        profile = getattr(request.user, "counselor_profile", None)
        if profile is None or profile.status != CounselorProfile.STATUS_APPROVED:
            return False
        request.counselor_profile = profile
        return True


def _resolve_learner_profile(request, learner_profile_id=None):
    """The learner profile the request acts for. Precedence:
      1. an explicitly-requested profile (self OR dependent) — MUST belong to
         this account;
      2. the ACTIVE profile from the session token — counselling is
         profile-level, so a booking/intake/assessment belongs to whichever
         learner profile is currently in context (not always the default);
      3. the account default, as a fallback for non-learner contexts / no token.
    Returns None when nothing matches (never someone else's profile)."""
    from accounts.models import LearnerProfile
    from accounts.auth_flow import get_active_profile

    user = request.user
    owned = LearnerProfile.objects.filter(account=user, is_active=True)
    if learner_profile_id:
        return owned.filter(pk=learner_profile_id).first()
    active = get_active_profile(request)
    if active is not None:
        return active
    if hasattr(user, "default_learner_profile"):
        lp = user.default_learner_profile()
        if lp is not None:
            return lp
    return owned.filter(is_default=True).first() or owned.first()


def _account_learner_ids(user):
    from accounts.models import LearnerProfile
    return list(
        LearnerProfile.objects.filter(account=user).values_list("id", flat=True)
    )


def _default_template():
    return (
        AssessmentTemplate.objects.filter(is_default=True).first()
        or AssessmentTemplate.objects.first()
    )


def _grant_counselor_role(user, approved_by=None):
    """Attach the COUNSELOR role to the account (idempotent). Uses the
    same Role/UserRole tables the rest of the platform uses, so
    user.has_role('COUNSELOR') — and forum COUNSELOR badges — work."""
    from accounts.models import Role, UserRole

    role, _ = Role.objects.get_or_create(name="COUNSELOR")
    defaults = {"is_active": True}
    ur, created = UserRole.objects.get_or_create(
        user=user, role=role, defaults=defaults
    )
    if not created and not ur.is_active:
        ur.is_active = True
        ur.save(update_fields=["is_active"])
    return ur


def _appointment_label(appt):
    return timezone.localtime(appt.scheduled_at).strftime("%d %b %Y, %I:%M %p")


# =====================================================
# PUBLIC — directory
# =====================================================

class ListSpecializationsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = Specialization.objects.filter(is_active=True)
        return Response(SpecializationSerializer(qs, many=True).data)


def _with_session_count(qs):
    """Annotate completed-appointment count per counselor.

    A Subquery (not a joined Count(filter=...)) because the directory
    queryset already joins `specializations` (M2M, with `.distinct()`) —
    stacking a second joined aggregate on top of that would fan out across
    both relations and inflate the count.
    """
    completed = (
        Appointment.objects.filter(
            counselor_id=OuterRef("pk"), status=Appointment.STATUS_COMPLETED
        )
        .values("counselor_id")
        .annotate(c=Count("id"))
        .values("c")
    )
    return qs.annotate(
        session_count=Coalesce(Subquery(completed, output_field=IntegerField()), 0)
    )


class CounselorDirectoryView(APIView):
    """Public counselor directory (approved + listed only)."""

    permission_classes = [AllowAny]

    def get(self, request):
        qs = CounselorProfile.objects.filter(
            status=CounselorProfile.STATUS_APPROVED, is_listed=True
        ).prefetch_related("specializations")
        qs = _with_session_count(qs)

        spec = request.query_params.get("specialization")
        if spec:
            qs = qs.filter(specializations__name__iexact=spec)

        language = request.query_params.get("language")
        if language:
            qs = qs.filter(languages__icontains=language)

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(display_name__icontains=search)
                | Q(bio__icontains=search)
                | Q(qualifications__icontains=search)
                | Q(specializations__name__icontains=search)
            )

        qs = qs.distinct().order_by("-avg_rating", "created_at")
        serializer = CounselorCardSerializer(
            qs, many=True, context={"request": request}
        )
        return Response({"results": serializer.data, "count": qs.count()})


class CounselorDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, counselor_id):
        qs = _with_session_count(
            CounselorProfile.objects.prefetch_related("specializations", "availability")
        )
        profile = get_object_or_404(
            qs,
            pk=counselor_id,
            status=CounselorProfile.STATUS_APPROVED,
            is_listed=True,
        )
        return Response(
            CounselorDetailSerializer(profile, context={"request": request}).data
        )


class PublicStatsView(APIView):
    """Site-wide counselling stats for the landing page hero — approved,
    listed counselors only, same visibility rule as the directory."""

    permission_classes = [AllowAny]

    def get(self, request):
        qs = CounselorProfile.objects.filter(
            status=CounselorProfile.STATUS_APPROVED, is_listed=True
        )
        agg = qs.aggregate(avg_rating=Avg("avg_rating"), counselor_count=Count("id"))
        total_sessions = Appointment.objects.filter(
            status=Appointment.STATUS_COMPLETED, counselor__in=qs
        ).count()
        return Response({
            "counselor_count": agg["counselor_count"] or 0,
            "avg_rating": round(float(agg["avg_rating"] or 0), 1),
            "total_sessions": total_sessions,
        })


class CounselorSlotsView(APIView):
    """Concrete bookable datetimes for the next N days."""

    permission_classes = [AllowAny]

    def get(self, request, counselor_id):
        profile = get_object_or_404(
            CounselorProfile, pk=counselor_id,
            status=CounselorProfile.STATUS_APPROVED, is_listed=True,
        )
        try:
            days = min(max(1, int(request.query_params.get("days", 14))), 30)
        except (TypeError, ValueError):
            days = 14
        slots = services.bookable_slots(profile, days=days)
        return Response({
            "counselor_id": profile.id,
            "duration_minutes": profile.session_duration_minutes,
            "slots": [timezone.localtime(s).isoformat() for s in slots],
        })


# =====================================================
# STUDENT — intake, matching
# =====================================================

class IntakeView(APIView):
    """GET returns (creating if needed) the intake for the acting learner
    profile; PUT updates it. Marked complete once at least one career
    interest is chosen — the gate before recommendations, per the spec."""

    permission_classes = [IsAuthenticated]

    def _intake(self, request):
        lp = _resolve_learner_profile(
            request,
            request.query_params.get("learner_profile_id")
            or request.data.get("learner_profile_id"),
        )
        if lp is None:
            return None, Response(
                {"detail": "No learner profile on this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        intake, _ = CounselingIntake.objects.get_or_create(learner_profile=lp)
        return intake, None

    def get(self, request):
        intake, err = self._intake(request)
        if err:
            return err
        return Response(IntakeSerializer(intake).data)

    def put(self, request):
        intake, err = self._intake(request)
        if err:
            return err
        serializer = IntakeSerializer(intake, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        intake = serializer.save()

        ids = request.data.get("career_interest_ids")
        if ids is not None:
            intake.career_interests.set(
                Specialization.objects.filter(id__in=ids, is_active=True)
            )
        if intake.career_interests.exists() and intake.completed_at is None:
            intake.completed_at = timezone.now()
            intake.save(update_fields=["completed_at"])
        return Response(IntakeSerializer(intake).data)


class MatchView(APIView):
    """Rule-based counselor recommendations for the acting learner profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        lp = _resolve_learner_profile(
            request, request.query_params.get("learner_profile_id")
        )
        if lp is None:
            return Response(
                {"detail": "No learner profile on this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        intake = getattr(lp, "counseling_intake", None)
        ranked = services.match_counselors(lp)
        serializer = MatchedCounselorSerializer(
            ranked, many=True, context={"request": request}
        )
        return Response({
            "results": serializer.data,
            "intake_complete": bool(intake and intake.is_complete),
            "learner_profile_id": str(lp.id),
        })


# =====================================================
# STUDENT — appointments
# =====================================================

class CreateAppointmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreateAppointmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        counselor = get_object_or_404(
            CounselorProfile, pk=data["counselor_id"],
            status=CounselorProfile.STATUS_APPROVED, is_listed=True,
        )
        lp = _resolve_learner_profile(request, data.get("learner_profile_id"))
        if lp is None:
            return Response(
                {"detail": "No learner profile on this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        when = data["scheduled_at"]
        duration = counselor.session_duration_minutes or 45

        # Lock the counselor row for the whole check-then-book critical
        # section — without this, two concurrent requests for the same
        # slot both pass booking_conflict() (neither has committed yet)
        # and both create a CONFIRMED appointment. Same pattern as the
        # already-fixed skill-booking races (skills/views.py's
        # ExpertProfile lock).
        with transaction.atomic():
            counselor = CounselorProfile.objects.select_for_update().get(pk=counselor.pk)

            if not services.inside_availability(counselor, when, duration):
                return Response(
                    {"detail": "That time is outside the counselor's availability."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if services.booking_conflict(counselor, when, duration):
                return Response(
                    {"detail": "That slot was just booked — pick another time."},
                    status=status.HTTP_409_CONFLICT,
                )

            appt = Appointment.objects.create(
                learner_profile=lp,
                booked_by=request.user,
                counselor=counselor,
                scheduled_at=when,
                duration_minutes=duration,
                student_note=data.get("student_note", ""),
            )

        # Start the assessment right away (draft) so the dashboard can show
        # "complete your assessment before the session" per the spec.
        template = _default_template()
        if template is not None:
            AssessmentResponse.objects.create(
                appointment=appt, learner_profile=lp, template=template
            )

        label = _appointment_label(appt)
        sms_when = timezone.localtime(appt.scheduled_at).strftime("%I:%M %p, %d %b")
        notify(
            recipient=counselor.user,
            actor=request.user,
            verb="counseling.booked",
            title=f"New session booked: {lp.display_name}",
            body=f"{lp.display_name} booked a session on {label}.",
            link_url=f"/counselor/appointments/{appt.id}",
            payload={"appointment_id": appt.id},
            audience_role="COUNSELOR",
            email=True,
            sms_vars={"title": f"with {lp.display_name}"[:30], "when": sms_when},
        )
        notify(
            recipient=request.user,
            verb="counseling.booked",
            title="Appointment confirmed",
            body=f"Your session with {counselor.display_name} is on {label}.",
            link_url=f"/counseling/appointments/{appt.id}",
            payload={"appointment_id": appt.id},
            audience_role="STUDENT",
            email=True,
            sms_vars={"title": f"with {counselor.display_name}"[:30], "when": sms_when},
            learner_profile=lp,
        )

        return Response(
            AppointmentSerializer(appt, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MyAppointmentsView(APIView):
    """Appointments for EVERY learner profile on the account — the parent
    sees their own and their dependents' sessions in one list."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Appointment.objects.filter(
            learner_profile_id__in=_account_learner_ids(request.user)
        ).select_related("counselor", "learner_profile").prefetch_related(
            "counselor__specializations"
        )
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        if request.query_params.get("upcoming") in ("1", "true"):
            qs = qs.filter(
                status=Appointment.STATUS_CONFIRMED,
                scheduled_at__gte=timezone.now(),
            ).order_by("scheduled_at")
        serializer = AppointmentSerializer(
            qs, many=True, context={"request": request}
        )
        return Response({"results": serializer.data, "count": qs.count()})


class CancelAppointmentView(APIView):
    """Either side cancels: the booking account or the counselor."""

    permission_classes = [IsAuthenticated]

    def post(self, request, appointment_id):
        appt = get_object_or_404(
            Appointment.objects.select_related("counselor", "learner_profile"),
            pk=appointment_id,
        )
        # Profile-scoped, not just account-scoped — a sibling must not be able
        # to cancel another child's counselling session.
        is_booker = _owns_appointment(request, appt)
        is_counselor = appt.counselor.user_id == request.user.id
        if not (is_booker or is_counselor):
            return Response(
                {"detail": "Not your appointment."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if appt.status != Appointment.STATUS_CONFIRMED:
            return Response(
                {"detail": f"This appointment is already {appt.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        appt.status = Appointment.STATUS_CANCELLED
        appt.cancel_reason = (request.data.get("reason") or "")[:300]
        appt.cancelled_by = request.user
        appt.save(update_fields=["status", "cancel_reason", "cancelled_by"])

        label = _appointment_label(appt)
        sms_when = timezone.localtime(appt.scheduled_at).strftime("%I:%M %p, %d %b")
        if is_counselor:
            notify(
                recipient=appt.booked_by,
                actor=request.user,
                verb="counseling.cancelled",
                title="Session cancelled by your counselor",
                body=f"Your session on {label} with {appt.counselor.display_name} was cancelled."
                     + (f' Reason: "{appt.cancel_reason}"' if appt.cancel_reason else ""),
                link_url="/counseling/appointments",
                payload={"appointment_id": appt.id},
                audience_role="STUDENT",
                email=True,
                sms_vars={"title": f"with {appt.counselor.display_name}"[:30],
                          "when": sms_when},
                learner_profile=appt.learner_profile,
            )
        else:
            notify(
                recipient=appt.counselor.user,
                actor=request.user,
                verb="counseling.cancelled",
                title=f"Session cancelled: {appt.learner_profile.display_name}",
                body=f"The session on {label} was cancelled by the student.",
                link_url="/counselor/appointments",
                payload={"appointment_id": appt.id},
                audience_role="COUNSELOR",
                sms_vars={"title": f"with {appt.learner_profile.display_name}"[:30],
                          "when": sms_when},
            )
        return Response({"detail": "Appointment cancelled.", "status": appt.status})


# =====================================================
# STUDENT — assessment & reports
# =====================================================

def _owns_appointment(request, appt):
    """May this REQUEST act on this appointment?

    Account ownership alone is not enough. On a one-email/many-children
    account, `appt.learner_profile.account_id == request.user.id` is true for
    EVERY sibling, so child A in learner context could read, overwrite and
    submit child B's career assessment, and cancel B's session — the most
    sensitive data in this app.

    When a learner profile is in context, the appointment must belong to THAT
    profile. With no learner profile in context (a parent managing from the
    account level, or a counsellor-side call), fall back to account ownership,
    which is the pre-existing behaviour and is correct there.

    Mirrors sessions_app/views.py's _get_owned_session, which fixed exactly
    this "a sibling could cancel another child's session" bug.
    """
    from accounts.auth_flow import get_active_profile

    if appt.booked_by_id != request.user.id and appt.learner_profile.account_id != request.user.id:
        return False
    active = get_active_profile(request)
    if active is not None and appt.learner_profile_id != active.id:
        return False
    return True


def _assessment_for_student(request, appointment_id):
    appt = get_object_or_404(
        Appointment.objects.select_related("learner_profile"), pk=appointment_id)
    if not _owns_appointment(request, appt):
        return None, Response({"detail": "Not your appointment."},
                              status=status.HTTP_403_FORBIDDEN)
    assessment = getattr(appt, "assessment", None)
    if assessment is None:
        template = _default_template()
        if template is None:
            return None, Response(
                {"detail": "No assessment template configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        assessment = AssessmentResponse.objects.create(
            appointment=appt, learner_profile=appt.learner_profile, template=template
        )
    return assessment, None


class AssessmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, appointment_id):
        assessment, err = _assessment_for_student(request, appointment_id)
        if err:
            return err
        return Response(AssessmentSerializer(assessment).data)

    def put(self, request, appointment_id):
        assessment, err = _assessment_for_student(request, appointment_id)
        if err:
            return err
        if assessment.status == AssessmentResponse.STATUS_SUBMITTED:
            return Response(
                {"detail": "Already submitted — it's with your counselor."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        answers = request.data.get("answers")
        if not isinstance(answers, dict):
            return Response({"detail": "answers must be an object."},
                            status=status.HTTP_400_BAD_REQUEST)
        assessment.answers = {**assessment.answers, **answers}
        assessment.save(update_fields=["answers", "updated_at"])
        return Response(AssessmentSerializer(assessment).data)


class SubmitAssessmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, appointment_id):
        assessment, err = _assessment_for_student(request, appointment_id)
        if err:
            return err
        if assessment.status == AssessmentResponse.STATUS_SUBMITTED:
            return Response({"detail": "Already submitted."},
                            status=status.HTTP_400_BAD_REQUEST)
        assessment.status = AssessmentResponse.STATUS_SUBMITTED
        assessment.submitted_at = timezone.now()
        assessment.save(update_fields=["status", "submitted_at"])

        appt = assessment.appointment
        notify(
            recipient=appt.counselor.user,
            actor=request.user,
            verb="counseling.assessment",
            title=f"Assessment submitted: {appt.learner_profile.display_name}",
            body=f"The career assessment for the session on {_appointment_label(appt)} is ready to review.",
            link_url=f"/counselor/appointments/{appt.id}",
            payload={"appointment_id": appt.id},
            audience_role="COUNSELOR",
        )
        return Response({"detail": "Assessment submitted.", "status": assessment.status})


class MyReportsView(APIView):
    """Published reports across all the account's learner profiles."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = SessionReport.objects.filter(
            is_published=True,
            appointment__learner_profile_id__in=_account_learner_ids(request.user),
        ).select_related("appointment__learner_profile", "counselor").order_by("-published_at")
        serializer = SessionReportSerializer(
            qs, many=True, context={"request": request}
        )
        return Response({"results": serializer.data, "count": qs.count()})


# =====================================================
# COUNSELOR — onboarding & profile
# =====================================================

class ApplyCounselorView(APIView):
    """Anyone signed in can apply. Creates a PENDING CounselorProfile that
    admins review — mirror of the teacher-approvals flow. Approval grants
    the COUNSELOR role."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if hasattr(request.user, "counselor_profile"):
            return Response(
                {"detail": "You already have a counselor profile.",
                 "status": request.user.counselor_profile.status},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = {k: v for k, v in request.data.items() if k != "user"}
        data.setdefault("display_name", request.user.username)
        serializer = CounselorSelfSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save(user=request.user)

        notify(
            recipient=request.user,
            verb="counseling.application",
            title="Counselor application received",
            body="Our team will review your profile and get back to you.",
            link_url="/counselor/apply",
            payload={"counselor_profile_id": profile.id},
        )
        return Response(
            CounselorSelfSerializer(profile).data, status=status.HTTP_201_CREATED
        )


class CounselorMeView(APIView):
    """The counselor's own profile — visible in any status so an applicant
    can watch/edit their pending application."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = getattr(request.user, "counselor_profile", None)
        if profile is None:
            return Response({"detail": "No counselor profile."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(CounselorSelfSerializer(profile).data)

    def put(self, request):
        profile = getattr(request.user, "counselor_profile", None)
        if profile is None:
            return Response({"detail": "No counselor profile."},
                            status=status.HTTP_404_NOT_FOUND)
        serializer = CounselorSelfSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(CounselorSelfSerializer(serializer.save()).data)


class AvailabilityView(APIView):
    permission_classes = [IsApprovedCounselor]

    def get(self, request):
        qs = request.counselor_profile.availability.all()
        return Response(AvailabilitySlotSerializer(qs, many=True).data)

    def post(self, request):
        serializer = AvailabilitySlotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        slot = serializer.save(counselor=request.counselor_profile)
        return Response(AvailabilitySlotSerializer(slot).data,
                        status=status.HTTP_201_CREATED)


class AvailabilityDeleteView(APIView):
    permission_classes = [IsApprovedCounselor]

    def delete(self, request, slot_id):
        slot = get_object_or_404(
            AvailabilitySlot, pk=slot_id, counselor=request.counselor_profile
        )
        slot.delete()
        return Response({"detail": "Slot removed."},
                        status=status.HTTP_204_NO_CONTENT)


# =====================================================
# COUNSELOR — sessions
# =====================================================

class CounselorAppointmentsView(APIView):
    permission_classes = [IsApprovedCounselor]

    def get(self, request):
        qs = Appointment.objects.filter(
            counselor=request.counselor_profile
        ).select_related("learner_profile", "counselor")
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        if request.query_params.get("upcoming") in ("1", "true"):
            qs = qs.filter(
                status=Appointment.STATUS_CONFIRMED,
                scheduled_at__gte=timezone.now(),
            ).order_by("scheduled_at")
        serializer = AppointmentSerializer(qs, many=True, context={"request": request})
        return Response({"results": serializer.data, "count": qs.count()})


def _own_appointment(request, appointment_id):
    return get_object_or_404(
        Appointment.objects.select_related("learner_profile", "counselor"),
        pk=appointment_id,
        counselor=request.counselor_profile,
    )


class SetMeetingLinkView(APIView):
    permission_classes = [IsApprovedCounselor]

    def post(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        link = (request.data.get("meeting_link") or "").strip()
        appt.meeting_link = link
        appt.save(update_fields=["meeting_link"])
        if link:
            notify(
                recipient=appt.booked_by,
                actor=request.user,
                verb="counseling.meeting_link",
                title="Your session link is ready",
                body=f"{appt.counselor.display_name} added the meeting link for {_appointment_label(appt)}.",
                link_url=f"/counseling/appointments/{appt.id}",
                payload={"appointment_id": appt.id, "meeting_link": link},
                audience_role="STUDENT",
            )
        return Response({"detail": "Meeting link saved.", "meeting_link": link})


class CompleteAppointmentView(APIView):
    permission_classes = [IsApprovedCounselor]

    def post(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        if appt.status != Appointment.STATUS_CONFIRMED:
            return Response({"detail": f"Appointment is {appt.status}."},
                            status=status.HTTP_400_BAD_REQUEST)
        no_show = request.data.get("no_show") in (True, "1", "true")
        appt.status = (
            Appointment.STATUS_NO_SHOW if no_show else Appointment.STATUS_COMPLETED
        )
        appt.save(update_fields=["status"])
        return Response({"detail": "Updated.", "status": appt.status})


class CounselorStudentView(APIView):
    """Everything the counselor needs before a session: learner context,
    intake, and the submitted assessment. Draft assessments stay private
    to the student until submitted."""

    permission_classes = [IsApprovedCounselor]

    def get(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        lp = appt.learner_profile

        intake = getattr(lp, "counseling_intake", None)
        assessment = getattr(appt, "assessment", None)
        submitted = (
            assessment is not None
            and assessment.status == AssessmentResponse.STATUS_SUBMITTED
        )

        return Response({
            "appointment": AppointmentSerializer(
                appt, context={"request": request}).data,
            "learner": {
                "id": str(lp.id),
                "display_name": lp.display_name,
                "current_class": getattr(lp, "current_class", ""),
                "stream": getattr(lp, "stream", ""),
                "board": getattr(lp, "board", ""),
                "gender": getattr(lp, "gender", ""),
                "school_name": getattr(lp, "school_name", ""),
            },
            "intake": IntakeSerializer(intake).data if intake else None,
            "assessment": (
                AssessmentSerializer(assessment).data if submitted else None
            ),
            "assessment_status": assessment.status if assessment else "missing",
        })


class SessionNotesView(APIView):
    """Counselor-private notes. Students can never read these."""

    permission_classes = [IsApprovedCounselor]

    def get(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        notes = appt.notes.filter(counselor=request.counselor_profile)
        return Response(SessionNoteSerializer(notes, many=True).data)

    def post(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        content = (request.data.get("content") or "").strip()
        if not content:
            return Response({"detail": "content is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = SessionNote.objects.create(
            appointment=appt, counselor=request.counselor_profile, content=content
        )
        return Response(SessionNoteSerializer(note).data,
                        status=status.HTTP_201_CREATED)


class SessionReportView(APIView):
    """GET/PUT the report for one appointment. PUT with publish=true makes
    it visible to the student and notifies them (bell + email). Published
    reports stay editable but cannot be unpublished (the student already
    saw it — retract by editing, not by hiding)."""

    permission_classes = [IsApprovedCounselor]

    def get(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        report = getattr(appt, "report", None)
        if report is None:
            return Response({"detail": "No report yet."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(SessionReportSerializer(
            report, context={"request": request}).data)

    def put(self, request, appointment_id):
        appt = _own_appointment(request, appointment_id)
        report, _ = SessionReport.objects.get_or_create(
            appointment=appt, defaults={"counselor": request.counselor_profile}
        )
        serializer = SessionReportSerializer(
            report, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        report = serializer.save()

        publish = request.data.get("publish") in (True, "1", "true")
        if publish and not report.is_published:
            report.is_published = True
            report.published_at = timezone.now()
            report.save(update_fields=["is_published", "published_at"])
            notify(
                recipient=appt.booked_by,
                actor=request.user,
                verb="counseling.report",
                title=f"Your counseling report is ready",
                body=f"{appt.counselor.display_name} published the report for your session on {_appointment_label(appt)}.",
                link_url=f"/counseling/reports",
                payload={"appointment_id": appt.id, "report_id": report.id},
                audience_role="STUDENT",
                email=True,
            )
        return Response(SessionReportSerializer(
            report, context={"request": request}).data)


# =====================================================
# ADMIN — approvals & oversight
# =====================================================

class AdminApplicationsView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        qs = CounselorProfile.objects.select_related("user").prefetch_related(
            "specializations"
        )
        status_f = request.query_params.get("status", "pending")
        if status_f in dict(CounselorProfile.STATUS_CHOICES):
            qs = qs.filter(status=status_f)
        stats = {
            "pending": CounselorProfile.objects.filter(
                status=CounselorProfile.STATUS_PENDING).count(),
            "approved": CounselorProfile.objects.filter(
                status=CounselorProfile.STATUS_APPROVED).count(),
        }
        return Response({
            "results": AdminCounselorApplicationSerializer(qs, many=True).data,
            "count": qs.count(),
            "stats": stats,
        })


class AdminApplicationActionView(APIView):
    """approve · reject · suspend · relist. Approve grants the COUNSELOR
    role; suspend hides from the directory and blocks the counselor
    console (status gate) without deleting anything."""

    permission_classes = [IsAdmin]

    ACTIONS = ("approve", "reject", "suspend", "relist")

    def post(self, request, profile_id):
        profile = get_object_or_404(
            CounselorProfile.objects.select_related("user"), pk=profile_id
        )
        action = request.data.get("action")
        note = (request.data.get("note") or "")[:300]
        if action not in self.ACTIONS:
            return Response({"detail": f"Unknown action '{action}'."},
                            status=status.HTTP_400_BAD_REQUEST)

        if action == "approve":
            profile.status = CounselorProfile.STATUS_APPROVED
            profile.is_listed = True
            _grant_counselor_role(profile.user, approved_by=request.user)
            notify(
                recipient=profile.user,
                actor=request.user,
                verb="counseling.approved",
                title="You're approved as a counselor 🎉",
                body="Your counselor profile is live. Set your weekly availability to start receiving bookings.",
                link_url="/counselor/availability",
                audience_role="COUNSELOR",
                email=True,
            )
        elif action == "reject":
            profile.status = CounselorProfile.STATUS_REJECTED
            notify(
                recipient=profile.user,
                actor=request.user,
                verb="counseling.rejected",
                title="Counselor application update",
                body="Your application wasn't approved this time."
                     + (f' Note: "{note}"' if note else ""),
                link_url="/counselor/apply",
                email=True,
            )
        elif action == "suspend":
            profile.status = CounselorProfile.STATUS_SUSPENDED
            profile.is_listed = False
        elif action == "relist":
            profile.status = CounselorProfile.STATUS_APPROVED
            profile.is_listed = True

        profile.review_note = note
        profile.reviewed_by = request.user
        profile.reviewed_at = timezone.now()
        profile.save()
        return Response({"detail": f"{action} done.", "status": profile.status,
                         "is_listed": profile.is_listed})


class AdminAppointmentsView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        qs = Appointment.objects.select_related(
            "counselor", "learner_profile", "booked_by"
        ).prefetch_related("counselor__specializations")
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(counselor__display_name__icontains=search)
                | Q(learner_profile__display_name__icontains=search)
                | Q(booked_by__email__icontains=search)
            )
        now = timezone.now()
        stats = {
            "upcoming": Appointment.objects.filter(
                status=Appointment.STATUS_CONFIRMED, scheduled_at__gte=now).count(),
            "completed": Appointment.objects.filter(
                status=Appointment.STATUS_COMPLETED).count(),
            "counselors": CounselorProfile.objects.filter(
                status=CounselorProfile.STATUS_APPROVED).count(),
        }
        return Response({
            "results": AdminAppointmentSerializer(
                qs[:200], many=True, context={"request": request}).data,
            "count": qs.count(),
            "stats": stats,
        })
