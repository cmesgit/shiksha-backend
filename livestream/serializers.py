from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
import uuid

from .models import LiveSession
from courses.models import Subject


from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
import uuid

from .models import LiveSession
from courses.models import Subject


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

        # ✅ 1️⃣ Role Check (FIXED TO MATCH YOUR SYSTEM)
        if not user.has_role("TEACHER"):
            raise serializers.ValidationError(
                {"non_field_errors": ["Only teachers can schedule sessions."]}
            )

        # ✅ 2️⃣ Subject Validation
        try:
            subject = Subject.objects.select_related("course").get(
                id=data["subject_id"]
            )
        except Subject.DoesNotExist:
            raise serializers.ValidationError(
                {"subject_id": ["Invalid subject."]}
            )

        # ✅ 3️⃣ Subject Assignment Check
        if not subject.subject_teachers.filter(teacher=user).exists():
            raise serializers.ValidationError(
                {"non_field_errors": ["You are not assigned to this subject."]}
            )

        start_time = data["start_time"]
        end_time = data["end_time"]
        now = timezone.now()

        # ✅ 4️⃣ Time Validation
        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]}
            )

        if start_time <= now:
            raise serializers.ValidationError(
                {"start_time": ["Cannot schedule a session in the past."]}
            )

        # ✅ 5️⃣ Overlapping Protection
        overlap_exists = LiveSession.objects.filter(
            subject=subject
        ).filter(
            Q(start_time__lt=end_time) &
            Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                {"non_field_errors": [
                    "This session overlaps with an existing session."]}
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
        ]

    def get_computed_status(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return "CANCELLED"

        if now < obj.start_time:
            return "SCHEDULED"

        if obj.start_time <= now <= obj.end_time:
            return "LIVE"

        return "COMPLETED"

    def get_can_join(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return False

        # Teacher can always join
        request = self.context.get("request")
        if request and request.user.has_role("TEACHER"):
            return True

        # Students can join 10 minutes before start
        return (
            obj.start_time - timedelta(minutes=10)
            <= now
            <= obj.end_time
        )
