from rest_framework import serializers
from .models import PrivateSession, SessionParticipant, ChatMessage


# ---------------------------------------------------------------------------
# Helpers — safely traverse user → profile → field
# Matches accounts.User (UUID pk, AbstractUser) + accounts.Profile
# ---------------------------------------------------------------------------

def get_user_name(user):
    """Return display name: profile.full_name → get_full_name() → username."""
    if user is None:
        return "Unknown"
    profile = getattr(user, "profile", None)
    if profile:
        name = getattr(profile, "full_name", None)
        if name:
            return name
    full = user.get_full_name()
    return full if full else user.username


def get_student_id(user):
    """Return profile.student_id or None."""
    profile = getattr(user, "profile", None)
    if profile:
        return getattr(profile, "student_id", None)
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
        fields = ["id", "user_id", "name", "student_id", "role", "joined_at", "left_at"]

    def get_name(self, obj):
        return get_user_name(obj.user)

    def get_student_id(self, obj):
        return get_student_id(obj.user)

    def get_user_id(self, obj):
        return str(obj.user_id)


# ---------------------------------------------------------------------------
# List serializer (lightweight, used in dashboard lists)
# ---------------------------------------------------------------------------

class SessionListSerializer(serializers.ModelSerializer):
    teacher_name = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()
    teacher_id = serializers.SerializerMethodField()
    requested_by_id = serializers.SerializerMethodField()
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
            "started_at",
            "ended_at",
            "actual_duration_minutes",
            "teacher_name",
            "teacher_id",
            "student_name",
            "student_id",
            "requested_by_id",
            "created_at",
        ]

    def get_teacher_name(self, obj):
        return get_user_name(obj.teacher)

    def get_student_name(self, obj):
        return get_user_name(obj.requested_by)

    def get_student_id(self, obj):
        return get_student_id(obj.requested_by)

    def get_teacher_id(self, obj):
        return str(obj.teacher_id)

    def get_requested_by_id(self, obj):
        return str(obj.requested_by_id)

    def get_actual_duration_minutes(self, obj):
        """Compute actual duration from started_at / ended_at timestamps."""
        if obj.started_at and obj.ended_at:
            delta = obj.ended_at - obj.started_at
            return max(1, round(delta.total_seconds() / 60))
        return None


# ---------------------------------------------------------------------------
# Detail serializer (full data, used in session detail views)
# ---------------------------------------------------------------------------

class PrivateSessionSerializer(serializers.ModelSerializer):
    teacher_name = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()
    teacher_id = serializers.SerializerMethodField()
    requested_by_id = serializers.SerializerMethodField()
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
        return get_user_name(obj.requested_by)

    def get_student_id(self, obj):
        return get_student_id(obj.requested_by)

    def get_teacher_id(self, obj):
        return str(obj.teacher_id)

    def get_requested_by_id(self, obj):
        return str(obj.requested_by_id)

    def get_actual_duration_minutes(self, obj):
        """Compute actual duration from started_at / ended_at timestamps."""
        if obj.started_at and obj.ended_at:
            delta = obj.ended_at - obj.started_at
            return max(1, round(delta.total_seconds() / 60))
        return None


# ---------------------------------------------------------------------------
# Request creation serializer (student submits a new session request)
# ---------------------------------------------------------------------------

class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "sender_name", "sender_role", "message", "created_at"]
        read_only_fields = ["id", "sender_name", "sender_role", "created_at"]


class SessionRequestSerializer(serializers.Serializer):
    # teacher_id is UUID string to match accounts.User.id
    teacher_id = serializers.UUIDField()
    subject = serializers.CharField(max_length=255)
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