from rest_framework import serializers
from django.utils import timezone
from datetime import datetime

from .models import (
    PrivateSession,
    SessionParticipant,
    ChatMessage,
    PrivateSessionReview,
    PrivateSessionNote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_learner(user):
    """The account's default (else first) learner profile, or None.

    Personal data moved off the deleted Profile model onto LearnerProfile.
    """
    if user is None or not hasattr(user, "learner_profiles"):
        return None
    return (
        user.learner_profiles.filter(is_default=True).first()
        or user.learner_profiles.first()
    )


def get_user_name(user):
    if user is None:
        return "Unknown"
    full = (user.get_full_name() or "").strip()
    if full:
        return full
    lp = _default_learner(user)
    if lp:
        name = (lp.full_name or "").strip() or lp.display_name
        if name:
            return name
    return user.username


def get_student_id(user):
    lp = _default_learner(user)
    return getattr(lp, "student_id", None) if lp else None


def session_student_name(obj):
    """Name of the learner the session is for.

    Prefers the booking `learner_profile` (so two children on one account are
    distinguishable); falls back to the account's default profile for legacy
    rows created before per-profile attribution.
    """
    lp = getattr(obj, "learner_profile", None)
    if lp is not None:
        name = (lp.full_name or "").strip() or lp.display_name
        if name:
            return name
    return get_user_name(obj.requested_by)


def session_student_id(obj):
    lp = getattr(obj, "learner_profile", None)
    if lp is not None:
        return getattr(lp, "student_id", None) or get_student_id(obj.requested_by)
    return get_student_id(obj.requested_by)


def calculate_duration_minutes(obj):
    if obj.started_at and obj.ended_at:
        delta = obj.ended_at - obj.started_at
        return max(1, round(delta.total_seconds() / 60))
    return None


# ---------------------------------------------------------------------------
# Participant serializer
# ---------------------------------------------------------------------------

class ParticipantSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()

    class Meta:
        model = SessionParticipant
        fields = ["id", "user_id", "name", "student_id",
                  "role", "joined_at", "left_at","status"]

    def get_name(self, obj):
        return get_user_name(obj.user)

    def get_student_id(self, obj):
        return get_student_id(obj.user)

    def get_user_id(self, obj):
        return str(obj.user_id)


# ---------------------------------------------------------------------------
# List serializer
# ---------------------------------------------------------------------------

class SessionListSerializer(serializers.ModelSerializer):
    teacher_name = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()
    teacher_id = serializers.SerializerMethodField()
    requested_by_id = serializers.SerializerMethodField()
    learner_profile_id = serializers.SerializerMethodField()
    actual_duration_minutes = serializers.SerializerMethodField()

    class Meta:
        model = PrivateSession
        fields = [
            "id",
            "subject",
            "status",
            "session_type",
            "group_strength",
            "scheduled_date",
            "scheduled_time",
            "duration_minutes",
            "rescheduled_date",
            "rescheduled_time",
            "reschedule_reason",
            "notes",
            "decline_reason",
            "cancel_reason",
            "started_at",
            "ended_at",
            "actual_duration_minutes",
            "teacher_name",
            "teacher_id",
            "student_name",
            "student_id",
            "requested_by_id",
            "learner_profile_id",
            "created_at",
        ]

    def get_teacher_name(self, obj):
        return get_user_name(obj.teacher)

    def get_student_name(self, obj):
        return session_student_name(obj)

    def get_student_id(self, obj):
        return session_student_id(obj)

    def get_learner_profile_id(self, obj):
        return str(obj.learner_profile_id) if obj.learner_profile_id else None

    def get_teacher_id(self, obj):
        return str(obj.teacher_id) if obj.teacher_id else None

    def get_requested_by_id(self, obj):
        return str(obj.requested_by_id) if obj.requested_by_id else None

    def get_actual_duration_minutes(self, obj):
        return calculate_duration_minutes(obj)


# ---------------------------------------------------------------------------
# Detail serializer
# ---------------------------------------------------------------------------

class PrivateSessionSerializer(serializers.ModelSerializer):
    teacher_name = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()
    teacher_id = serializers.SerializerMethodField()
    requested_by_id = serializers.SerializerMethodField()
    learner_profile_id = serializers.SerializerMethodField()
    participants = ParticipantSerializer(many=True, read_only=True)
    actual_duration_minutes = serializers.SerializerMethodField()

    class Meta:
        model = PrivateSession
        fields = [
            "id",
            "subject",
            "status",
            "session_type",
            "group_strength",
            "scheduled_date",
            "scheduled_time",
            "duration_minutes",
            "rescheduled_date",
            "rescheduled_time",
            "reschedule_reason",
            "notes",
            "decline_reason",
            "cancel_reason",
            "room_name",
            "teacher_name",
            "teacher_id",
            "student_name",
            "student_id",
            "requested_by_id",
            "learner_profile_id",
            "participants",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "actual_duration_minutes",
        ]

    def get_teacher_name(self, obj):
        return get_user_name(obj.teacher)

    def get_student_name(self, obj):
        return session_student_name(obj)

    def get_student_id(self, obj):
        return session_student_id(obj)

    def get_learner_profile_id(self, obj):
        return str(obj.learner_profile_id) if obj.learner_profile_id else None

    def get_teacher_id(self, obj):
        return str(obj.teacher_id) if obj.teacher_id else None

    def get_requested_by_id(self, obj):
        return str(obj.requested_by_id) if obj.requested_by_id else None

    def get_actual_duration_minutes(self, obj):
        return calculate_duration_minutes(obj)


# ---------------------------------------------------------------------------
# Chat serializer
# ---------------------------------------------------------------------------

class ChatMessageSerializer(serializers.ModelSerializer):
    sender_id = serializers.CharField(source="sender.id", read_only=True)

    class Meta:
        model = ChatMessage
        fields = ["id", "sender_id", "sender_name",
                  "sender_role", "message", "created_at"]
        read_only_fields = ["id", "sender_id",
                            "sender_name", "sender_role", "created_at"]


class PrivateSessionReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrivateSessionReview
        fields = ["id", "rating", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class PrivateSessionNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrivateSessionNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]


# ---------------------------------------------------------------------------
# Request serializer
# ---------------------------------------------------------------------------

class SessionRequestSerializer(serializers.Serializer):
    teacher_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()  # ✅ FIXED
    scheduled_date = serializers.DateField()
    scheduled_time = serializers.TimeField()
    duration_minutes = serializers.IntegerField(default=60)
    session_type = serializers.ChoiceField(
        choices=["one_on_one", "group"], default="one_on_one"
    )
    group_strength = serializers.IntegerField(default=1)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    student_ids = serializers.ListField(
        child=serializers.CharField(), required=False, default=[]
    )

    def validate(self, data):
        from django.utils import timezone
        from datetime import datetime

        scheduled_dt = timezone.make_aware(
            datetime.combine(data["scheduled_date"], data["scheduled_time"])
        )

        if scheduled_dt < timezone.now():
            raise serializers.ValidationError("Cannot schedule in the past.")

        if data["duration_minutes"] <= 0:
            raise serializers.ValidationError("Invalid duration.")

        if data["session_type"] == "group" and data["group_strength"] <= 1:
            raise serializers.ValidationError("Group must be >1.")

        return data
