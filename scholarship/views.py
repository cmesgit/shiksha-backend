"""
scholarship/views.py — student-facing endpoints for the Instant Scholarship
flow (course selection reuses the existing /api/courses/ endpoints; this app
starts at identity verification and runs through to the award).
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth_flow import get_active_profile
from accounts.permissions import IsEmailVerified
from courses.models import Course

from . import aadhaar_offline, services
from .models import (
    ExamQuestion,
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)
from .permissions import HasActiveLearnerProfile
from .serializers import (
    AnswerWriteSerializer,
    CheatSignalSerializer,
    EligibilityCheckSerializer,
    ExamQuestionStudentSerializer,
    ExamResultSerializer,
    ExamSessionSerializer,
    GuardianVerificationCreateSerializer,
    GuardianVerificationStatusSerializer,
    ScholarshipAwardSerializer,
)


class PublicScholarshipConfigView(APIView):
    """GET /api/scholarship/config/ — the safe-to-show subset of
    ScholarshipSettings + the current band table, so the marketing/flow
    screens (calculator, instructions, exam header) reflect real admin
    config instead of hardcoded numbers that would silently drift. Excludes
    anything an admin might reasonably not want public (active_kyc_provider,
    anti-cheat thresholds)."""
    permission_classes = [AllowAny]

    def get(self, request):
        settings_obj = ScholarshipSettings.load()
        bands = list(
            ScholarshipBand.objects.filter(is_active=True).order_by("-min_correct")
            .values("min_correct", "max_correct", "discount_pct")
        )
        max_discount = max((b["discount_pct"] for b in bands), default=0)
        return Response({
            "enabled": settings_obj.enabled,
            "question_count": settings_obj.question_count,
            "duration_minutes": settings_obj.duration_minutes,
            "max_discount_pct": max_discount,
            "bands": bands,
            "subjects": [c[0] for c in ScholarshipQuestionBankItem.SUBJECT_CHOICES],
            "difficulty_split": {
                "easy": settings_obj.difficulty_easy_pct,
                "medium": settings_obj.difficulty_medium_pct,
                "hard": settings_obj.difficulty_hard_pct,
            },
            "verification_methods": {
                "digilocker": settings_obj.allow_digilocker,
                "aadhaar_otp": settings_obj.allow_aadhaar_otp,
                "aadhaar_offline": settings_obj.allow_aadhaar_offline,
                "manual": settings_obj.allow_manual_review,
            },
        })


def _client_meta(request):
    return {
        "ip_address": request.META.get("REMOTE_ADDR", ""),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
    }


def _parse_ddmmyyyy(value):
    """Offline e-KYC XML dates are DD-MM-YYYY (see UIDAI's sample:
    dob="02-11-1995"). Returns None rather than raising — a DOB the model
    can't parse just means verified_adult_dob stays blank, not a 500."""
    from datetime import datetime
    try:
        return datetime.strptime(value, "%d-%m-%Y").date()
    except (ValueError, TypeError):
        return None


# ── Guardian verification ────────────────────────────────────────────────

class GuardianVerificationCreateView(APIView):
    """POST /api/scholarship/verification/

    The account making this call IS the parent/guardian — submitting this
    request is the DPDP Rule 10 consent act, so consent metadata is stamped
    here rather than via a separate confirmation step.

    - digilocker/aadhaar_otp: creates a pending record. Actually exchanging
      a provider code for a verified identity happens in a vendor-specific
      callback that doesn't exist yet — see the module docstring on
      GuardianVerification and enrollments/payments.py's RazorpayProvider
      for the same "documented stub until a vendor is chosen" pattern.
    - manual: creates a pending record for admin review
      (GuardianVerificationActionView).
    - aadhaar_offline: verified SYNCHRONOUSLY, right here, against UIDAI's
      own published signing certificate — see scholarship/aadhaar_offline.py
      for the full compliance/security rationale. No vendor, no waiting.
    """
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        serializer = GuardianVerificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = serializer.validated_data["method"]

        settings_obj = ScholarshipSettings.load()
        kwargs = dict(
            account=request.user,
            method=method,
            consent_given_at=timezone.now(),
            consent_ip=request.META.get("REMOTE_ADDR"),
            consent_user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        )

        if method == GuardianVerification.METHOD_MANUAL:
            document = request.FILES.get("manual_document")
            if not document:
                return Response(
                    {"manual_document": "A document is required for manual review."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            kwargs["manual_document"] = document

        elif method == GuardianVerification.METHOD_AADHAAR_OFFLINE:
            zip_file = request.FILES.get("ekyc_zip")
            share_code = (request.data.get("share_code") or "").strip()
            if not zip_file or not share_code:
                return Response(
                    {"detail": "Both the e-KYC ZIP file and its share code are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                verified = aadhaar_offline.verify_offline_ekyc(zip_file.read(), share_code)
            except aadhaar_offline.AadhaarOfflineVerificationError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            kwargs.update(
                status=GuardianVerification.STATUS_VERIFIED,
                provider="uidai_offline_ekyc",
                provider_reference=aadhaar_offline.dedup_reference_for(verified),
                verified_adult_name=verified["name"],
                reviewed_at=timezone.now(),
            )
            dob = _parse_ddmmyyyy(verified["dob"])
            if dob:
                kwargs["verified_adult_dob"] = dob

        else:
            kwargs["provider"] = settings_obj.active_kyc_provider

        record = GuardianVerification.objects.create(**kwargs)
        return Response(
            GuardianVerificationStatusSerializer(record).data, status=status.HTTP_201_CREATED
        )


class GuardianVerificationStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        record = (
            GuardianVerification.objects.filter(account=request.user).order_by("-created_at").first()
        )
        if record is None:
            return Response({"detail": "No verification on file."}, status=status.HTTP_404_NOT_FOUND)
        return Response(GuardianVerificationStatusSerializer(record).data)


# ── Eligibility ──────────────────────────────────────────────────────────

class EligibilityCheckView(APIView):
    """POST /api/scholarship/eligibility/check/   { course_id }

    Requires a VERIFIED guardian record on the account and an active learner
    profile. Idempotent — safe to call again before the exam is started."""
    permission_classes = [IsAuthenticated, IsEmailVerified, HasActiveLearnerProfile]

    def post(self, request):
        serializer = EligibilityCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        course = serializer.validated_data["course_id"]

        settings_obj = ScholarshipSettings.load()
        if not settings_obj.enabled:
            return Response({"eligible": False, "reason": "scholarship_disabled"}, status=status.HTTP_400_BAD_REQUEST)

        guardian_verification = (
            GuardianVerification.objects.filter(
                account=request.user, status=GuardianVerification.STATUS_VERIFIED
            ).order_by("-created_at").first()
        )
        if guardian_verification is None:
            return Response({"eligible": False, "reason": "identity_not_verified"}, status=status.HTTP_400_BAD_REQUEST)

        learner = get_active_profile(request)
        if str(learner.current_class or "") != str(course.class_level or ""):
            return Response(
                {"eligible": False, "reason": "class_mismatch",
                 "detail": "The selected course's class doesn't match this student's current class."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        academic_year = learner.academic_year
        if not academic_year:
            return Response({"eligible": False, "reason": "missing_academic_year"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            record = services.get_or_reserve_eligibility(
                learner_profile=learner, guardian_verification=guardian_verification, academic_year=academic_year,
            )
        except services.AlreadyAttemptedError:
            return Response(
                {"eligible": False, "reason": "already_attempted",
                 "detail": "One scholarship attempt is available per academic year."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "eligible": True,
            "eligibility_record_id": str(record.id),
            "academic_year": academic_year,
            "class_level": course.class_level,
        })


# ── Exam ─────────────────────────────────────────────────────────────────

class ExamStartView(APIView):
    """POST /api/scholarship/exam/start/   { eligibility_record_id, course_id }

    Idempotent via ExamSession's OneToOne to the eligibility record — calling
    this again just returns the same in-progress session (resume)."""
    permission_classes = [IsAuthenticated, IsEmailVerified, HasActiveLearnerProfile]

    def post(self, request):
        learner = get_active_profile(request)
        record = get_object_or_404(
            ScholarshipEligibilityRecord, pk=request.data.get("eligibility_record_id"), learner_profile=learner,
        )
        if record.status == ScholarshipEligibilityRecord.STATUS_VOIDED:
            raise PermissionDenied("This eligibility record was voided.")

        course = get_object_or_404(Course, pk=request.data.get("course_id"))
        meta = _client_meta(request)
        try:
            session, _created = services.start_or_resume_exam_session(
                record, course,
                ip_address=meta["ip_address"], user_agent=meta["user_agent"],
                device_fingerprint=request.data.get("device_fingerprint", "")[:128],
            )
        except services.InsufficientQuestionBankError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        session = services.expire_if_past_deadline(session)
        return Response(ExamSessionSerializer(session).data, status=status.HTTP_200_OK)


class ExamSessionDetailView(APIView):
    """GET /api/scholarship/exam/session/<id>/ — polled by the client for the
    server-authoritative deadline and answered-count."""
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request, session_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        session = services.expire_if_past_deadline(session)
        return Response(ExamSessionSerializer(session).data)


class CurrentExamSessionView(APIView):
    """GET /api/scholarship/exam/session/current/

    What the landing page's resume banner reads: is there a still-live
    session for the active learner profile, without already knowing its id.
    404 if none — that's a normal, expected response, not an error state."""
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request):
        learner = get_active_profile(request)
        session = (
            ExamSession.objects.filter(learner_profile=learner, status=ExamSession.STATUS_IN_PROGRESS)
            .order_by("-started_at").first()
        )
        if session is None:
            return Response({"detail": "No live session."}, status=status.HTTP_404_NOT_FOUND)
        session = services.expire_if_past_deadline(session)
        if session.status != ExamSession.STATUS_IN_PROGRESS:
            return Response({"detail": "No live session."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ExamSessionSerializer(session).data)


class ExamQuestionListView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request, session_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        session = services.expire_if_past_deadline(session)
        questions = session.questions.select_related("answer").order_by("order")
        return Response(ExamQuestionStudentSerializer(questions, many=True).data)


class ExamAnswerView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def _get_session_and_question(self, request, session_id, question_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        question = get_object_or_404(ExamQuestion, pk=question_id, session=session)
        return session, question

    def patch(self, request, session_id, question_id):
        session, question = self._get_session_and_question(request, session_id, question_id)
        session = services.expire_if_past_deadline(session)
        if session.status != ExamSession.STATUS_IN_PROGRESS:
            return Response({"detail": "This exam is no longer in progress."}, status=status.HTTP_409_CONFLICT)

        serializer = AnswerWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.record_answer(session, question, **serializer.validated_data)
        except services.DeadlinePassedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"saved": True})

    def delete(self, request, session_id, question_id):
        session, question = self._get_session_and_question(request, session_id, question_id)
        session = services.expire_if_past_deadline(session)
        try:
            services.clear_answer(session, question)
        except services.DeadlinePassedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"cleared": True})


class ExamCheatSignalView(APIView):
    """POST /api/scholarship/exam/session/<id>/cheat-signal/

    Fire-and-forget from the client (tab-hidden, focus-lost, paste, ...).
    Never blocks the exam — logging + flagging only, per the 'strong but
    invisible' anti-cheat brief."""
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        serializer = CheatSignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.log_cheat_signal(session, **serializer.validated_data)
        return Response({"logged": True})


class ExamSubmitView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        session = services.submit_exam(session)
        return Response(ExamResultSerializer(session).data)


class ExamResultView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request, session_id):
        learner = get_active_profile(request)
        session = get_object_or_404(ExamSession, pk=session_id, learner_profile=learner)
        session = services.expire_if_past_deadline(session)
        if session.status not in (ExamSession.STATUS_SUBMITTED, ExamSession.STATUS_EXPIRED):
            return Response({"detail": "Not yet submitted."}, status=status.HTTP_409_CONFLICT)
        return Response(ExamResultSerializer(session).data)


# ── Awards ───────────────────────────────────────────────────────────────

class MyAwardsView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request):
        learner = get_active_profile(request)
        awards = ScholarshipAward.objects.filter(learner_profile=learner).order_by("-created_at")
        return Response(ScholarshipAwardSerializer(awards, many=True).data)


class AwardDetailView(APIView):
    permission_classes = [IsAuthenticated, HasActiveLearnerProfile]

    def get(self, request, award_id):
        learner = get_active_profile(request)
        award = get_object_or_404(ScholarshipAward, pk=award_id, learner_profile=learner)
        return Response(ScholarshipAwardSerializer(award).data)
