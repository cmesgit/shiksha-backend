from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
import uuid
from zoneinfo import ZoneInfo

from .models import LiveSession
from courses.models import Subject

IST = ZoneInfo("Asia/Kolkata")


class LiveSessionCreateSerializer(serializers.ModelSerializer):
    subject_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "description",
            "start_time",
            "end_time",
            "subject_id",
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        request = self.context.get("request")
        user = request.user

        if not user.has_role("TEACHER"):
            raise serializers.ValidationError(
                {"non_field_errors": ["Only teachers can schedule sessions."]}
            )

        try:
            subject = Subject.objects.select_related("course").get(
                id=data["subject_id"]
            )
        except Subject.DoesNotExist:
            raise serializers.ValidationError(
                {"subject_id": ["Invalid subject."]}
            )

        if not subject.subject_teachers.filter(teacher=user).exists():
            raise serializers.ValidationError(
                {"non_field_errors": ["You are not assigned to this subject."]}
            )

        start_time = data["start_time"]
        end_time = data["end_time"]

        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time, IST)

        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time, IST)

        data["start_time"] = start_time
        data["end_time"] = end_time

        now = timezone.now()

        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]}
            )

        if start_time <= now:
            raise serializers.ValidationError(
                {"start_time": ["Cannot schedule a session in the past."]}
            )

        overlap_exists = LiveSession.objects.filter(
            subject=subject
        ).filter(
            Q(start_time__lt=end_time) &
            Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                {"non_field_errors": [
                    "This session overlaps with an existing session."
                ]}
            )

        self._validated_subject = subject
        return data

    def create(self, validated_data):
        subject = self._validated_subject
        user = self.context["request"].user

        validated_data.pop("subject_id", None)

        room_name = f"session_{uuid.uuid4().hex}"

        return LiveSession.objects.create(
            subject=subject,
            course=subject.course,
            room_name=room_name,
            created_by=user,
            **validated_data
        )


class LiveSessionListSerializer(serializers.ModelSerializer):
    teacher = serializers.CharField(source="created_by.email", read_only=True)
    can_join = serializers.SerializerMethodField()
    computed_status = serializers.SerializerMethodField()

    subject_id = serializers.UUIDField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_name = serializers.CharField(source="course.title", read_only=True)

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "start_time",
            "end_time",
            "computed_status",
            "teacher",
            "can_join",
            "subject_id",
            "subject_name",
            "course_name",
        ]

    def get_computed_status(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return "CANCELLED"

        if obj.teacher_left_at:
            diff = now - obj.teacher_left_at
            if diff <= timedelta(minutes=10):
                return "RECONNECTING"
            if diff <= timedelta(minutes=60):
                return "PAUSED"
            return "COMPLETED"

        if obj.status == LiveSession.STATUS_LIVE:
            return "LIVE"

        if now < obj.start_time:
            return "SCHEDULED"

        return "WAITING_FOR_TEACHER"

    def get_can_join(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return False

        if obj.teacher_left_at:
            if now > obj.teacher_left_at + timedelta(minutes=60):
                return False

        request = self.context.get("request")

        if request and request.user.has_role("TEACHER"):
            return True

        return now >= obj.start_time - timedelta(minutes=15)
