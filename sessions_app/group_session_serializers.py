"""
Serializers for the Group Sessions feature.

Kept in a dedicated module so nothing about the existing private-session
serializers is affected.  Reuses `get_user_name` / `get_student_id` from
the original serializers module where useful.
"""

from rest_framework import serializers

from .models import GroupSession, GroupSessionInvite, GroupSessionNote, GroupSessionReview
from .serializers import get_user_name, get_student_id


# ---------------------------------------------------------------------------
# Invite serializer
# ---------------------------------------------------------------------------


class GroupSessionInviteSerializer(serializers.ModelSerializer):
    """Shape used inside the detail/list payload, from the host's POV."""

    user_id = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    student_id = serializers.SerializerMethodField()

    class Meta:
        model = GroupSessionInvite
        fields = [
            "id",
            "user_id",
            "name",
            "student_id",
            "invite_role",
            "status",
            "decline_count",
            "created_at",
            "responded_at",
            "reinvited_at",
            "joined_at",
        ]

    def get_user_id(self, obj):
        return str(obj.user_id)

    def get_name(self, obj):
        return get_user_name(obj.user)

    def get_student_id(self, obj):
        return get_student_id(obj.user)


# ---------------------------------------------------------------------------
# List + detail serializers
# ---------------------------------------------------------------------------


class GroupSessionListSerializer(serializers.ModelSerializer):
    """Card-view payload returned by /group-sessions/mine/ tabs."""

    host_name = serializers.SerializerMethodField()
    host_id = serializers.SerializerMethodField()
    subject_id = serializers.SerializerMethodField()
    course_id = serializers.SerializerMethodField()
    invited_teacher_name = serializers.SerializerMethodField()
    invited_teacher_id = serializers.SerializerMethodField()
    accepted_count = serializers.SerializerMethodField()
    pending_count = serializers.SerializerMethodField()
    declined_count = serializers.SerializerMethodField()

    class Meta:
        model = GroupSession
        fields = [
            "id",
            "short_code",
            "session_type",
            "admit_mode",
            "subject_id",
            "subject_name",
            "course_id",
            "course_title",
            "topic",
            "status",
            "scheduled_date",
            "scheduled_time",
            "duration_minutes",
            "max_invitees",
            "room_started_at",
            "ended_at",
            "room_name",
            "host_id",
            "host_name",
            "invited_teacher_id",
            "invited_teacher_name",
            "accepted_count",
            "pending_count",
            "declined_count",
            "created_at",
        ]

    def get_host_name(self, obj):
        return get_user_name(obj.host)

    def get_host_id(self, obj):
        return str(obj.host_id) if obj.host_id else None

    def get_subject_id(self, obj):
        return str(obj.subject_id) if obj.subject_id else None

    def get_course_id(self, obj):
        # Course is reachable via the subject FK; falling back to None keeps
        # this safe even if a legacy row has a null subject.
        try:
            return str(obj.subject.course_id) if obj.subject_id else None
        except Exception:
            return None

    def get_invited_teacher_name(self, obj):
        if obj.invited_teacher_id:
            return get_user_name(obj.invited_teacher)
        return None

    def get_invited_teacher_id(self, obj):
        return str(obj.invited_teacher_id) if obj.invited_teacher_id else None

    def _count_invites(self, obj, status_key):
        # Prefetched as `invites` by the view.
        return sum(1 for inv in obj.invites.all() if inv.status == status_key)

    def get_accepted_count(self, obj):
        return self._count_invites(obj, "accepted")

    def get_pending_count(self, obj):
        return self._count_invites(obj, "pending")

    def get_declined_count(self, obj):
        return self._count_invites(obj, "declined")


class GroupSessionDetailSerializer(GroupSessionListSerializer):
    """List shape + full invite list."""

    invites = GroupSessionInviteSerializer(many=True, read_only=True)

    class Meta(GroupSessionListSerializer.Meta):
        fields = GroupSessionListSerializer.Meta.fields + ["invites"]


# ---------------------------------------------------------------------------
# Create-group input serializer
# ---------------------------------------------------------------------------


class GroupSessionCreateSerializer(serializers.Serializer):
    """
    Input for POST /group-sessions/create/.

    * subject_id must belong to a course the host is enrolled in.
    * invited_teacher_id must teach `subject_id`.
    * invited_user_ids must all be students enrolled in the same course.
    * duration must be 30 / 45 / 60.
    * 1 <= len(invited_user_ids) <= 50  (need at least 1 to be invitable;
      room won't actually open until at least 1 accepts).
    """

    subject_id = serializers.UUIDField()
    scheduled_date = serializers.DateField()
    scheduled_time = serializers.TimeField()
    duration_minutes = serializers.ChoiceField(choices=[30, 45, 60, 90, 120, 150, 180])
    topic = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    invited_teacher_id = serializers.UUIDField(required=False, allow_null=True)
    invited_user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=50,
    )

    def validate(self, data):
        from django.utils import timezone
        from datetime import datetime

        scheduled_dt = timezone.make_aware(
            datetime.combine(data["scheduled_date"], data["scheduled_time"])
        )
        if scheduled_dt < timezone.now():
            raise serializers.ValidationError(
                {"scheduled_date": "Cannot schedule in the past."}
            )

        # dedupe invitees
        data["invited_user_ids"] = list(
            {str(uid) for uid in data["invited_user_ids"]}
        )

        if len(data["invited_user_ids"]) > 50:
            raise serializers.ValidationError(
                {"invited_user_ids": "Maximum 50 invitees."}
            )
        return data


# ---------------------------------------------------------------------------
# Invite-more input serializer
# ---------------------------------------------------------------------------


class GroupSessionInviteMoreSerializer(serializers.Serializer):
    invited_user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
    )


class GroupSessionUpdateSerializer(serializers.Serializer):
    """
    PATCH /group-sessions/<id>/ — edit a session while it's still
    ``scheduled``. Topic/date/time/duration only: subject is fixed once a
    session exists (existing invites are validated against the subject's
    course, so changing it after invites exist would leave them validated
    against the wrong course) — a subject change would need to be a new
    session, not an edit of this one.
    """

    scheduled_date = serializers.DateField(required=False)
    scheduled_time = serializers.TimeField(required=False)
    duration_minutes = serializers.ChoiceField(
        choices=[30, 45, 60, 90, 120, 150, 180], required=False
    )
    topic = serializers.CharField(
        required=False, allow_blank=True, max_length=255
    )

    def validate(self, data):
        from django.utils import timezone
        from datetime import datetime

        session = self.context["session"]
        date = data.get("scheduled_date", session.scheduled_date)
        time_ = data.get("scheduled_time", session.scheduled_time)
        scheduled_dt = timezone.make_aware(datetime.combine(date, time_))
        if scheduled_dt < timezone.now():
            raise serializers.ValidationError(
                {"scheduled_date": "Cannot reschedule to a time in the past."}
            )
        return data


class GroupSessionNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupSessionNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]


class GroupSessionReviewSerializer(serializers.ModelSerializer):
    """NEW — design_handoff_live_sessions Phase 5, screen 09. Mirrors
    ``PrivateSessionReviewSerializer`` (sessions_app/serializers.py)."""

    class Meta:
        model = GroupSessionReview
        fields = ["id", "rating", "description", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
