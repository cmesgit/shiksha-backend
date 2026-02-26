from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
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

        # ---- 1️⃣ Role Check ----
        if not user.has_role("teacher"):
            raise serializers.ValidationError(
                "Only teachers can schedule sessions."
            )

        # ---- 2️⃣ Subject Validation ----
        try:
            subject = Subject.objects.select_related("course").get(
                id=data["subject_id"]
            )
        except Subject.DoesNotExist:
            raise serializers.ValidationError("Invalid subject.")

        # ---- 3️⃣ Subject Assignment Check ----
        if not subject.teachers.filter(id=user.id).exists():
            raise serializers.ValidationError(
                "You are not assigned to this subject."
            )

        start_time = data["start_time"]
        end_time = data["end_time"]
        now = timezone.now()

        # ---- 4️⃣ Time Validation ----
        if start_time >= end_time:
            raise serializers.ValidationError(
                "End time must be after start time."
            )

        if start_time < now:
            raise serializers.ValidationError(
                "Cannot schedule a session in the past."
            )

        # ---- 5️⃣ Overlapping Session Protection ----
        overlap_exists = LiveSession.objects.filter(
            subject=subject
        ).filter(
            Q(start_time__lt=end_time) &
            Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                "This session overlaps with an existing session."
            )

        # Store subject for create()
        self._validated_subject = subject

        return data

    def create(self, validated_data):
        subject = self._validated_subject
        user = self.context["request"].user

        # ---- 6️⃣ Safe Unique Room Name ----
        room_name = f"session_{uuid.uuid4().hex}"

        return LiveSession.objects.create(
            subject=subject,
            course=subject.course,
            room_name=room_name,
            created_by=user,
            **validated_data
        )
