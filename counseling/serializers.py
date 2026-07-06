# PLACEMENT: backend/backend/counseling/serializers.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/serializers.py

from django.utils import timezone
from rest_framework import serializers

from .models import (
    Appointment, AssessmentResponse, AssessmentTemplate, AvailabilitySlot,
    CounselingIntake, CounselorProfile, SessionNote, SessionReport,
    Specialization,
)


class SpecializationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specialization
        fields = ("id", "name")


# ── Counselor: public card / profile ────────────────────────────────────

class CounselorCardSerializer(serializers.ModelSerializer):
    """Directory card — matches the spec's recommended-counselor card:
    photo, name, qualifications, expertise, experience, languages, rating."""

    specializations = SpecializationSerializer(many=True, read_only=True)
    languages = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = CounselorProfile
        fields = (
            "id", "display_name", "photo_url", "qualifications",
            "years_experience", "languages", "specializations",
            "avg_rating", "rating_count", "session_duration_minutes",
        )

    def get_languages(self, obj):
        return obj.language_list()

    def get_photo_url(self, obj):
        if not obj.photo:
            return ""
        request = self.context.get("request")
        url = obj.photo.url
        return request.build_absolute_uri(url) if request else url


class CounselorDetailSerializer(CounselorCardSerializer):
    availability = serializers.SerializerMethodField()

    class Meta(CounselorCardSerializer.Meta):
        fields = CounselorCardSerializer.Meta.fields + (
            "bio", "certifications", "approach", "availability",
        )

    def get_availability(self, obj):
        return [
            {
                "weekday": w.weekday,
                "weekday_label": w.get_weekday_display(),
                "start": w.start_time.strftime("%H:%M"),
                "end": w.end_time.strftime("%H:%M"),
            }
            for w in obj.availability.filter(is_active=True)
        ]


class MatchedCounselorSerializer(serializers.Serializer):
    """One row of the recommendation list."""
    counselor = CounselorCardSerializer(source="profile")
    match_score = serializers.IntegerField(source="score")
    reasons = serializers.ListField(child=serializers.CharField())


# ── Counselor: self-managed profile ─────────────────────────────────────

class CounselorSelfSerializer(serializers.ModelSerializer):
    specializations = SpecializationSerializer(many=True, read_only=True)
    specialization_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False
    )

    class Meta:
        model = CounselorProfile
        fields = (
            "id", "display_name", "bio", "qualifications", "certifications",
            "approach", "years_experience", "languages", "specializations",
            "specialization_ids", "session_duration_minutes",
            "status", "is_listed", "review_note", "avg_rating", "rating_count",
        )
        read_only_fields = ("status", "is_listed", "review_note",
                            "avg_rating", "rating_count")

    def _apply_specializations(self, profile, ids):
        if ids is None:
            return
        profile.specializations.set(
            Specialization.objects.filter(id__in=ids, is_active=True)
        )

    def create(self, validated_data):
        ids = validated_data.pop("specialization_ids", None)
        profile = CounselorProfile.objects.create(**validated_data)
        self._apply_specializations(profile, ids)
        return profile

    def update(self, instance, validated_data):
        ids = validated_data.pop("specialization_ids", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        self._apply_specializations(instance, ids)
        return instance


class AvailabilitySlotSerializer(serializers.ModelSerializer):
    weekday_label = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = AvailabilitySlot
        fields = ("id", "weekday", "weekday_label", "start_time", "end_time", "is_active")

    def validate(self, data):
        start = data.get("start_time", getattr(self.instance, "start_time", None))
        end = data.get("end_time", getattr(self.instance, "end_time", None))
        if start and end and start >= end:
            raise serializers.ValidationError("start_time must be before end_time.")
        return data


# ── Intake ──────────────────────────────────────────────────────────────

class IntakeSerializer(serializers.ModelSerializer):
    career_interests = SpecializationSerializer(many=True, read_only=True)
    career_interest_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False
    )
    is_complete = serializers.BooleanField(read_only=True)
    # Read-only context from the LearnerProfile so the frontend can show
    # "we already know your class/stream" without another call.
    learner = serializers.SerializerMethodField()

    class Meta:
        model = CounselingIntake
        fields = (
            "id", "learner", "career_interests", "career_interest_ids",
            "preferred_industry", "work_environment", "long_term_goals",
            "short_term_goals", "skills", "languages", "favorite_subjects",
            "is_complete", "completed_at",
        )
        read_only_fields = ("completed_at",)

    def get_learner(self, obj):
        lp = obj.learner_profile
        return {
            "id": str(lp.id),
            "display_name": lp.display_name,
            "current_class": getattr(lp, "current_class", ""),
            "stream": getattr(lp, "stream", ""),
            "board": getattr(lp, "board", ""),
        }


# ── Appointments ────────────────────────────────────────────────────────

class AppointmentSerializer(serializers.ModelSerializer):
    counselor = CounselorCardSerializer(read_only=True)
    learner = serializers.SerializerMethodField()
    end_at = serializers.DateTimeField(read_only=True)
    has_assessment = serializers.SerializerMethodField()
    assessment_submitted = serializers.SerializerMethodField()
    has_report = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = (
            "id", "counselor", "learner", "scheduled_at", "end_at",
            "duration_minutes", "status", "meeting_link", "student_note",
            "cancel_reason", "has_assessment", "assessment_submitted",
            "has_report", "created_at",
        )

    def get_learner(self, obj):
        lp = obj.learner_profile
        return {"id": str(lp.id), "display_name": lp.display_name}

    def _assessment(self, obj):
        try:
            return obj.assessment
        except AssessmentResponse.DoesNotExist:
            return None

    def get_has_assessment(self, obj):
        return self._assessment(obj) is not None

    def get_assessment_submitted(self, obj):
        a = self._assessment(obj)
        return bool(a and a.status == AssessmentResponse.STATUS_SUBMITTED)

    def get_has_report(self, obj):
        try:
            return obj.report.is_published
        except SessionReport.DoesNotExist:
            return False


class CreateAppointmentSerializer(serializers.Serializer):
    counselor_id = serializers.IntegerField()
    learner_profile_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    scheduled_at = serializers.DateTimeField()
    student_note = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)

    def validate_scheduled_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Pick a time in the future.")
        return value


# ── Assessment ──────────────────────────────────────────────────────────

class AssessmentSerializer(serializers.ModelSerializer):
    sections = serializers.SerializerMethodField()

    class Meta:
        model = AssessmentResponse
        fields = ("id", "appointment", "sections", "answers", "status", "submitted_at")
        read_only_fields = ("appointment", "status", "submitted_at")

    def get_sections(self, obj):
        return obj.template.sections


# ── Notes & reports ─────────────────────────────────────────────────────

class SessionNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionNote
        fields = ("id", "content", "created_at")


class SessionReportSerializer(serializers.ModelSerializer):
    counselor_name = serializers.CharField(source="counselor.display_name", read_only=True)
    appointment_at = serializers.DateTimeField(source="appointment.scheduled_at", read_only=True)
    learner = serializers.SerializerMethodField()
    attachment_url = serializers.SerializerMethodField()

    class Meta:
        model = SessionReport
        fields = (
            "id", "appointment", "appointment_at", "counselor_name", "learner",
            "summary", "recommendations", "next_steps", "attachment_url",
            "is_published", "published_at",
        )
        read_only_fields = ("appointment", "is_published", "published_at")

    def get_learner(self, obj):
        lp = obj.appointment.learner_profile
        return {"id": str(lp.id), "display_name": lp.display_name}

    def get_attachment_url(self, obj):
        if not obj.attachment:
            return ""
        request = self.context.get("request")
        url = obj.attachment.url
        return request.build_absolute_uri(url) if request else url


# ── Admin ───────────────────────────────────────────────────────────────

class AdminCounselorApplicationSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    specializations = SpecializationSerializer(many=True, read_only=True)

    class Meta:
        model = CounselorProfile
        fields = (
            "id", "display_name", "email", "username", "bio", "qualifications",
            "certifications", "approach", "years_experience", "languages",
            "specializations", "status", "is_listed", "review_note",
            "reviewed_at", "created_at",
        )


class AdminAppointmentSerializer(AppointmentSerializer):
    booked_by_email = serializers.EmailField(source="booked_by.email", read_only=True)

    class Meta(AppointmentSerializer.Meta):
        fields = AppointmentSerializer.Meta.fields + ("booked_by_email",)
