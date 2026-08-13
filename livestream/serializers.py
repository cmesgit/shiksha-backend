from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
import uuid
from zoneinfo import ZoneInfo

from .models import LiveSession, SessionReview, SessionNote
from courses.models import Subject, Batch
from courses.services import is_teacher_of

IST = ZoneInfo("Asia/Kolkata")


class LiveSessionCreateSerializer(serializers.ModelSerializer):
    subject_id = serializers.UUIDField(write_only=True)
    # A live class is a batch's timetable entry, so a batch is required for
    # new sessions (legacy batch=NULL rows stay valid — this is write-side only).
    batch_id = serializers.UUIDField(write_only=True)
    force_live = serializers.BooleanField(
        write_only=True, required=False, default=False)

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "description",
            "start_time",
            "end_time",
            "subject_id",
            "batch_id",
            "force_live",
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

        try:
            batch = Batch.objects.get(id=data["batch_id"])
        except Batch.DoesNotExist:
            raise serializers.ValidationError(
                {"batch_id": ["Invalid batch."]}
            )

        # Triangle guard: the subject and the batch must be the same course.
        if subject.course_id != batch.course_id:
            raise serializers.ValidationError(
                {"batch_id": ["Batch and subject belong to different courses."]}
            )

        # Authz: assigned to this (batch, subject) — either scoped to the
        # batch, or course-wide (is_teacher_of() covers both).
        if not is_teacher_of(user, batch, subject):
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

        force_live = data.pop("force_live", False)
        if not force_live and start_time <= now:
            raise serializers.ValidationError(
                {"start_time": ["Cannot schedule a session in the past."]}
            )

        # Overlap is scoped to this batch+subject: two different batches may
        # legitimately hold the same subject at the same time.
        overlap_exists = LiveSession.objects.filter(
            subject=subject, batch=batch,
        ).exclude(
            status__in=[LiveSession.STATUS_CANCELLED,
                        LiveSession.STATUS_COMPLETED]
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
        self._validated_batch = batch
        return data

    def create(self, validated_data):
        subject = self._validated_subject
        batch = self._validated_batch
        user = self.context["request"].user

        validated_data.pop("subject_id", None)
        validated_data.pop("batch_id", None)

        room_name = f"session_{uuid.uuid4().hex}"

        return LiveSession.objects.create(
            subject=subject,
            course=subject.course,
            batch=batch,
            room_name=room_name,
            created_by=user,
            **validated_data
        )


class LiveSessionUpdateSerializer(serializers.ModelSerializer):
    """
    Edit a session that hasn't started yet — title/description/time only.
    Subject and batch are fixed at creation; changing either would make this
    a different class's timetable entry, not a reschedule of this one.
    """

    class Meta:
        model = LiveSession
        fields = ["title", "description", "start_time", "end_time"]

    def validate(self, data):
        session = self.instance
        start_time = data.get("start_time", session.start_time)
        end_time = data.get("end_time", session.end_time)

        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time, IST)
        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time, IST)
        data["start_time"] = start_time
        data["end_time"] = end_time

        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]}
            )

        if start_time <= timezone.now():
            raise serializers.ValidationError(
                {"start_time": ["Cannot reschedule to a time in the past."]}
            )

        # Same overlap rule as create, scoped to this batch+subject and
        # excluding the session being edited (it always "overlaps" itself).
        overlap_exists = LiveSession.objects.filter(
            subject=session.subject, batch=session.batch,
        ).exclude(id=session.id).exclude(
            status__in=[LiveSession.STATUS_CANCELLED,
                        LiveSession.STATUS_COMPLETED]
        ).filter(
            Q(start_time__lt=end_time) & Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                {"non_field_errors": [
                    "This time overlaps with another session."
                ]}
            )

        return data


class LiveSessionListSerializer(serializers.ModelSerializer):
    teacher = serializers.CharField(source="created_by.email", read_only=True)
    can_join = serializers.SerializerMethodField()
    computed_status = serializers.SerializerMethodField()
    subject_id = serializers.UUIDField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_name = serializers.CharField(source="course.title", read_only=True)
    teacher_left_at = serializers.DateTimeField(read_only=True)
    status = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True, default=None)
    batch_student_count = serializers.SerializerMethodField()

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "description",
            "start_time",
            "end_time",
            "computed_status",
            "teacher",
            "can_join",
            "subject_id",
            "subject_name",
            "course_name",
            "teacher_left_at",
            "status",
            "batch_name",
            "batch_student_count",
        ]

    def get_batch_student_count(self, obj):
        if not obj.batch_id:
            return None
        from enrollments.models import Enrollment
        return Enrollment.objects.filter(
            batch_id=obj.batch_id, status=Enrollment.STATUS_ACTIVE
        ).count()

    def get_computed_status(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return "CANCELLED"

        if obj.status == LiveSession.STATUS_COMPLETED:
            return "COMPLETED"

        # Sessions past end_time always show as completed
        if now >= obj.end_time:
            return "COMPLETED"

        # Manual pause takes priority over teacher_left_at timer
        if obj.status == LiveSession.STATUS_PAUSED and not obj.teacher_left_at:
            return "PAUSED"

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

        # Allow joining paused sessions — student sees paused screen inside

        if obj.teacher_left_at:
            if now > obj.teacher_left_at + timedelta(minutes=60):
                return False

        request = self.context.get("request")

        if request and request.user.has_role("TEACHER"):
            return True

        return now >= obj.start_time - timedelta(minutes=15)


class SessionReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionReview
        fields = ["id", "rating", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class SessionNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]
