from rest_framework import serializers

from courses.models import Course

from .models import (
    CheatSignalEvent,
    ExamQuestion,
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)


class CourseSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "title", "class_level", "price", "mrp"]


# ── Guardian verification (student-facing) ──────────────────────────────

class GuardianVerificationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardianVerification
        fields = ["method"]

    def validate_method(self, value):
        settings_obj = ScholarshipSettings.load()
        allowed = {
            GuardianVerification.METHOD_DIGILOCKER: settings_obj.allow_digilocker,
            GuardianVerification.METHOD_AADHAAR_OTP: settings_obj.allow_aadhaar_otp,
            GuardianVerification.METHOD_AADHAAR_OFFLINE: settings_obj.allow_aadhaar_offline,
            GuardianVerification.METHOD_MANUAL: settings_obj.allow_manual_review,
        }
        if not allowed.get(value):
            raise serializers.ValidationError("This verification method isn't currently available.")
        return value


class GuardianVerificationStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardianVerification
        fields = ["id", "method", "status", "created_at", "rejection_reason"]
        read_only_fields = fields


# ── Eligibility ──────────────────────────────────────────────────────────

class EligibilityCheckSerializer(serializers.Serializer):
    course_id = serializers.PrimaryKeyRelatedField(queryset=Course.objects.all())


# ── Exam (student-facing — correct_option_index MUST NEVER appear here) ─

class ExamQuestionStudentSerializer(serializers.ModelSerializer):
    selected_option_index = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = ["id", "order", "subject", "difficulty", "text", "options", "selected_option_index"]
        # correct_option_index intentionally absent — server-only, scored on submit.

    def get_selected_option_index(self, obj):
        answer = getattr(obj, "answer", None)
        return answer.selected_option_index if answer else None


class ExamSessionSerializer(serializers.ModelSerializer):
    course = CourseSummarySerializer()
    deadline = serializers.DateTimeField()
    server_time = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    answered_count = serializers.SerializerMethodField()

    class Meta:
        model = ExamSession
        fields = [
            "id", "course", "status", "started_at", "deadline", "server_time",
            "question_count", "answered_count", "tab_switch_count",
        ]

    def get_server_time(self, obj):
        from django.utils import timezone
        return timezone.now()

    def get_question_count(self, obj):
        return obj.questions.count()

    def get_answered_count(self, obj):
        return obj.questions.filter(answer__selected_option_index__isnull=False).count()


class AnswerWriteSerializer(serializers.Serializer):
    selected_option_index = serializers.IntegerField(min_value=0, max_value=3)
    time_spent_seconds = serializers.IntegerField(min_value=0, default=0)


class CheatSignalSerializer(serializers.Serializer):
    event_type = serializers.ChoiceField(choices=CheatSignalEvent.EVENT_CHOICES)
    metadata = serializers.JSONField(required=False, default=dict)


class ExamResultSerializer(serializers.ModelSerializer):
    course = CourseSummarySerializer()
    award_id = serializers.SerializerMethodField()

    class Meta:
        model = ExamSession
        fields = ["id", "course", "score", "awarded_discount_pct", "subject_breakdown", "submitted_at", "award_id"]

    def get_award_id(self, obj):
        try:
            return str(obj.award.id)
        except ScholarshipAward.DoesNotExist:
            return None


class ScholarshipAwardSerializer(serializers.ModelSerializer):
    course = CourseSummarySerializer()

    class Meta:
        model = ScholarshipAward
        fields = ["id", "course", "discount_pct", "academic_year", "status", "expires_at", "redeemed_at"]


# ── Admin ────────────────────────────────────────────────────────────────

class ScholarshipSettingsAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScholarshipSettings
        exclude = ["singleton_id"]


class ScholarshipBandAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScholarshipBand
        fields = "__all__"

    def validate(self, attrs):
        min_c = attrs.get("min_correct", getattr(self.instance, "min_correct", None))
        max_c = attrs.get("max_correct", getattr(self.instance, "max_correct", None))
        if min_c is not None and max_c is not None and min_c > max_c:
            raise serializers.ValidationError("min_correct cannot exceed max_correct.")
        return attrs


class ScholarshipQuestionBankItemAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScholarshipQuestionBankItem
        fields = "__all__"
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def validate_options(self, value):
        if not isinstance(value, list) or len(value) != 4:
            raise serializers.ValidationError("options must be a list of exactly 4 strings.")
        return value

    def validate_correct_option_index(self, value):
        if not (0 <= value <= 3):
            raise serializers.ValidationError("correct_option_index must be 0-3.")
        return value


class GuardianVerificationAdminSerializer(serializers.ModelSerializer):
    account_email = serializers.EmailField(source="account.email", read_only=True)

    class Meta:
        model = GuardianVerification
        fields = [
            "id", "account", "account_email", "method", "status", "provider",
            "manual_document", "verified_adult_name", "verified_adult_dob",
            "rejection_reason", "reviewed_by", "reviewed_at", "created_at",
        ]
        read_only_fields = [f for f in fields if f not in ("status", "rejection_reason")]


class ExamSessionAdminSerializer(serializers.ModelSerializer):
    course_title = serializers.CharField(source="course.title", read_only=True)
    learner_name = serializers.CharField(source="learner_profile.display_name", read_only=True)

    class Meta:
        model = ExamSession
        fields = [
            "id", "learner_name", "course_title", "status", "started_at", "deadline",
            "submitted_at", "score", "awarded_discount_pct", "tab_switch_count",
            "flagged_for_review", "review_status", "reviewed_by", "reviewed_at", "review_notes",
        ]


class ScholarshipAwardAdminSerializer(serializers.ModelSerializer):
    """Distinct from the student-facing ScholarshipAwardSerializer, which
    deliberately omits learner_profile (a student only ever sees their own
    award) — an admin list is useless without knowing WHICH student an
    award belongs to."""
    course_title = serializers.CharField(source="course.title", read_only=True)
    learner_name = serializers.CharField(source="learner_profile.display_name", read_only=True)

    class Meta:
        model = ScholarshipAward
        fields = [
            "id", "learner_name", "course_title", "discount_pct", "academic_year", "status",
            "expires_at", "redeemed_at", "voided_by", "voided_at", "void_reason", "created_at",
        ]


class ScholarshipEligibilityRecordAdminSerializer(serializers.ModelSerializer):
    learner_name = serializers.CharField(source="learner_profile.display_name", read_only=True)

    class Meta:
        model = ScholarshipEligibilityRecord
        fields = [
            "id", "learner_name", "academic_year", "status", "voided_by",
            "voided_at", "void_reason", "created_at",
        ]
