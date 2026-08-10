"""
scholarship/admin_views.py — the admin API surface: settings, bands,
question bank (incl. AI drafting), verification review queue, exam session
monitor + void, eligibility ledger, awards, dashboard stats.
"""
import json
import logging

import requests
from django.conf import settings as django_settings
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from . import services
from .models import (
    CheatSignalEvent,
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)
from .serializers import (
    ExamSessionAdminSerializer,
    GuardianVerificationAdminSerializer,
    ScholarshipAwardAdminSerializer,
    ScholarshipBandAdminSerializer,
    ScholarshipEligibilityRecordAdminSerializer,
    ScholarshipQuestionBankItemAdminSerializer,
    ScholarshipSettingsAdminSerializer,
)

logger = logging.getLogger(__name__)


class ScholarshipSettingsAdminView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        return Response(ScholarshipSettingsAdminSerializer(ScholarshipSettings.load()).data)

    def patch(self, request):
        obj = ScholarshipSettings.load()
        serializer = ScholarshipSettingsAdminSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ScholarshipBandListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipBandAdminSerializer
    queryset = ScholarshipBand.objects.all()


class ScholarshipBandDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipBandAdminSerializer
    queryset = ScholarshipBand.objects.all()


class QuestionBankListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipQuestionBankItemAdminSerializer

    def get_queryset(self):
        qs = ScholarshipQuestionBankItem.objects.all().order_by("-created_at")
        params = self.request.query_params
        for field in ("class_level", "subject", "difficulty", "source"):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class QuestionBankDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipQuestionBankItemAdminSerializer
    queryset = ScholarshipQuestionBankItem.objects.all()


class QuestionBankGenerateAIView(APIView):
    """POST /admin/question-bank/generate-ai/  { class_level, subject, difficulty, count }

    Mirrors quizzes.views.TeacherGenerateAIQuestionsView: drafts are
    returned to the client only, nothing is written here — an admin reviews
    the drafts and posts the approved ones to QuestionBankBulkCreateView.
    Requires OPENAI_API_KEY.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "scholarship_ai_generate"

    def post(self, request):
        class_level = request.data.get("class_level")
        subject = request.data.get("subject")
        difficulty = request.data.get("difficulty", "medium")
        if not class_level or not subject:
            raise ValidationError("class_level and subject are required.")
        try:
            count = max(1, min(20, int(request.data.get("count") or 5)))
        except (TypeError, ValueError):
            count = 5

        api_key = getattr(django_settings, "OPENAI_API_KEY", "") or ""
        if not api_key:
            raise ValidationError("OPENAI_API_KEY is not configured; cannot generate questions.")

        subject_label = dict(ScholarshipQuestionBankItem.SUBJECT_CHOICES).get(subject, subject)
        schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                            "correct_option_index": {"type": "integer", "minimum": 0, "maximum": 3},
                            "explanation": {"type": "string"},
                        },
                        "required": ["text", "options", "correct_option_index", "explanation"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        }
        prompt = (
            f"Write {count} multiple-choice exam questions for an Indian Class "
            f"{class_level} student, subject \"{subject_label}\", at {difficulty} "
            f"difficulty, aligned with a typical CBSE/state-board syllabus. Each "
            f"question needs exactly 4 answer options with exactly one correct "
            f"answer (correct_option_index), and a short explanation. No regional "
            f"language content. Keep questions unambiguous and distractors plausible."
        )

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": "You are a precise exam-question writer for a school scholarship test."},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "scholarship_questions", "schema": schema, "strict": True},
                    },
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            drafted = json.loads(content)["questions"]
        except Exception:
            logger.exception("Scholarship AI question generation failed")
            raise ValidationError("AI generation failed. Try again or add questions manually.")

        for item in drafted:
            item["class_level"] = class_level
            item["subject"] = subject
            item["difficulty"] = difficulty
        return Response({"questions": drafted})


class QuestionBankBulkCreateView(APIView):
    """POST /admin/question-bank/bulk-create/  { questions: [...] }

    Persists admin-approved drafts (from generate-ai, or hand-authored) as
    inactive bank items — an explicit activate step (PATCH is_active=true on
    QuestionBankDetailView) is still required before they can be sampled
    into a real exam."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        items = request.data.get("questions", [])
        if not isinstance(items, list) or not items:
            raise ValidationError("questions must be a non-empty list.")

        created = []
        errors = []
        for i, raw in enumerate(items):
            serializer = ScholarshipQuestionBankItemAdminSerializer(data={
                **raw, "is_active": False, "source": ScholarshipQuestionBankItem.SOURCE_AI_GENERATED,
            })
            if serializer.is_valid():
                serializer.save(created_by=request.user)
                created.append(serializer.data)
            else:
                errors.append({"index": i, "errors": serializer.errors})

        return Response({"created": created, "errors": errors}, status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_201_CREATED)


class GuardianVerificationQueueView(generics.ListAPIView):
    """GET /admin/verifications/?status=pending"""
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = GuardianVerificationAdminSerializer

    def get_queryset(self):
        qs = GuardianVerification.objects.all().order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs


class GuardianVerificationActionView(APIView):
    """POST /admin/verifications/<id>/action/  { action: approve|reject, reason }

    Only the manual-review path reaches this today — digilocker/aadhaar_otp
    become verified via a vendor callback once a reseller is integrated
    (see views.GuardianVerificationCreateView's docstring)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, verification_id):
        record = get_object_or_404(GuardianVerification, pk=verification_id)
        action = request.data.get("action")
        if action == "approve":
            record.status = GuardianVerification.STATUS_VERIFIED
        elif action == "reject":
            record.status = GuardianVerification.STATUS_REJECTED
            record.rejection_reason = request.data.get("reason", "")[:300]
        else:
            raise ValidationError("action must be 'approve' or 'reject'.")
        record.reviewed_by = request.user
        record.reviewed_at = timezone.now()
        record.save(update_fields=["status", "rejection_reason", "reviewed_by", "reviewed_at"])
        return Response(GuardianVerificationAdminSerializer(record).data)


class ExamSessionMonitorView(generics.ListAPIView):
    """GET /admin/sessions/?flagged=true&status=in_progress"""
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ExamSessionAdminSerializer

    def get_queryset(self):
        qs = ExamSession.objects.select_related("course", "learner_profile").order_by("-started_at")
        params = self.request.query_params
        if params.get("flagged") in ("1", "true", "yes"):
            qs = qs.filter(flagged_for_review=True)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs


class ExamSessionDetailAdminView(APIView):
    """GET gives the full picture (session + cheat signals) for the review
    queue; POST acts on it (clear the flag, or void the session and any
    award it produced)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, session_id):
        session = get_object_or_404(ExamSession, pk=session_id)
        signals = list(
            CheatSignalEvent.objects.filter(session=session).values("event_type", "metadata", "created_at")
        )
        data = ExamSessionAdminSerializer(session).data
        data["cheat_signals"] = signals
        return Response(data)

    def post(self, request, session_id):
        session = get_object_or_404(ExamSession, pk=session_id)
        action = request.data.get("action")
        notes = request.data.get("notes", "")[:500]

        if action == "clear":
            session.review_status = ExamSession.REVIEW_CLEARED
        elif action == "void":
            session.review_status = ExamSession.REVIEW_VOIDED
            session.status = ExamSession.STATUS_VOIDED
            ScholarshipAward.objects.filter(exam_session=session).update(
                status=ScholarshipAward.STATUS_VOIDED, voided_by=request.user, voided_at=timezone.now(),
                void_reason=notes or "Exam session voided by admin review.",
            )
        else:
            raise ValidationError("action must be 'clear' or 'void'.")

        session.reviewed_by = request.user
        session.reviewed_at = timezone.now()
        session.review_notes = notes
        session.save(update_fields=["status", "review_status", "reviewed_by", "reviewed_at", "review_notes"])
        return Response(ExamSessionAdminSerializer(session).data)


class EligibilityLedgerView(generics.ListAPIView):
    """GET /admin/eligibility/?academic_year=2026-27 — support/appeals tool:
    'why can't this student retake the exam'."""
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipEligibilityRecordAdminSerializer

    def get_queryset(self):
        qs = ScholarshipEligibilityRecord.objects.select_related("learner_profile").order_by("-created_at")
        params = self.request.query_params
        if params.get("academic_year"):
            qs = qs.filter(academic_year=params["academic_year"])
        if params.get("learner_profile"):
            qs = qs.filter(learner_profile_id=params["learner_profile"])
        return qs


class EligibilityVoidView(APIView):
    """POST /admin/eligibility/<id>/void/  { reason }

    Frees the slot — e.g. after confirming an eligibility record was created
    in error. Does NOT touch any exam session/award already tied to it; void
    those separately via ExamSessionDetailAdminView if the whole attempt
    needs unwinding."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, record_id):
        record = get_object_or_404(ScholarshipEligibilityRecord, pk=record_id)
        record.status = ScholarshipEligibilityRecord.STATUS_VOIDED
        record.voided_by = request.user
        record.voided_at = timezone.now()
        record.void_reason = request.data.get("reason", "")[:300]
        record.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])
        return Response(ScholarshipEligibilityRecordAdminSerializer(record).data)


class AwardListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    serializer_class = ScholarshipAwardAdminSerializer

    def get_queryset(self):
        qs = ScholarshipAward.objects.select_related("course", "learner_profile").order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs


class AwardVoidView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, award_id):
        award = get_object_or_404(ScholarshipAward, pk=award_id)
        award.status = ScholarshipAward.STATUS_VOIDED
        award.voided_by = request.user
        award.voided_at = timezone.now()
        award.void_reason = request.data.get("reason", "")[:300]
        award.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])
        return Response(ScholarshipAwardAdminSerializer(award).data)


class ScholarshipDashboardStatsView(APIView):
    """GET /admin/stats/ — the numbers an admin dashboard's overview card
    needs: attempts, awards, flagged-for-review queue depth, redemption
    conversion."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        total_sessions = ExamSession.objects.count()
        submitted = ExamSession.objects.filter(
            status__in=[ExamSession.STATUS_SUBMITTED, ExamSession.STATUS_EXPIRED]
        ).count()
        flagged_open = ExamSession.objects.filter(flagged_for_review=True, review_status="").count()

        awards_qs = ScholarshipAward.objects.all()
        band_distribution = list(
            awards_qs.values("discount_pct").annotate(count=Count("id")).order_by("-discount_pct")
        )

        return Response({
            "total_sessions": total_sessions,
            "submitted_sessions": submitted,
            "flagged_for_review_open": flagged_open,
            "awards_total": awards_qs.count(),
            "awards_redeemed": awards_qs.filter(status=ScholarshipAward.STATUS_REDEEMED).count(),
            "awards_locked": awards_qs.filter(status=ScholarshipAward.STATUS_LOCKED).count(),
            "awards_active": awards_qs.filter(status=ScholarshipAward.STATUS_ACTIVE).count(),
            "band_distribution": band_distribution,
            "pending_verifications": GuardianVerification.objects.filter(
                status=GuardianVerification.STATUS_PENDING
            ).count(),
        })
