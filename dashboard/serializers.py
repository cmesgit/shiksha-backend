from activity.models import Activity
from quizzes.models import Quiz
from assignments.models import Assignment
from rest_framework import serializers
from livestream.models import LiveSession
from sessions_app.models import PrivateSession


class DashboardSessionSerializer(serializers.ModelSerializer):
    # LiveSession.created_by is nullable (on_delete=SET_NULL); fall back
    # to a safe string instead of raising AttributeError.
    subject = serializers.SerializerMethodField()
    subject_id = serializers.SerializerMethodField()
    topic = serializers.CharField(source="title")
    teacher = serializers.SerializerMethodField()
    dateTime = serializers.DateTimeField(source="start_time")

    class Meta:
        model = LiveSession
        fields = ["id", "subject", "subject_id", "topic", "teacher", "dateTime"]

    def get_subject(self, obj):
        try:
            return obj.subject.name if obj.subject_id else ""
        except Exception:
            return ""

    def get_subject_id(self, obj):
        try:
            return str(obj.subject_id) if obj.subject_id else None
        except Exception:
            return None

    def get_teacher(self, obj):
        try:
            return obj.created_by.email if obj.created_by_id else ""
        except Exception:
            return ""


class DashboardAssignmentSerializer(serializers.ModelSerializer):
    teacher = serializers.SerializerMethodField()
    due = serializers.DateTimeField(source="due_date")
    subject_id = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = ["id", "title", "teacher", "due", "subject_id", "subject_name"]

    def get_subject_id(self, obj):
        try:
            if obj.chapter_id and obj.chapter and obj.chapter.subject_id:
                return str(obj.chapter.subject.id)
            return None
        except Exception:
            return None

    def get_subject_name(self, obj):
        try:
            if obj.chapter_id and obj.chapter and obj.chapter.subject_id:
                return obj.chapter.subject.name
            return ""
        except Exception:
            return ""

    def get_teacher(self, obj):
        try:
            subject = obj.chapter.subject if obj.chapter_id else None
            if not subject:
                return "Unknown"
            teachers = getattr(subject, "prefetched_teachers", None)
            if teachers:
                t = teachers[0]
                if t and t.teacher_id:
                    return t.teacher.email
            teacher = subject.subject_teachers.first()
            if teacher and teacher.teacher_id:
                return teacher.teacher.email
        except Exception:
            pass
        return "Unknown"


class DashboardQuizSerializer(serializers.ModelSerializer):
    teacher = serializers.SerializerMethodField()
    due = serializers.DateTimeField(source="due_date")
    subject_id = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = ["id", "title", "teacher", "due", "subject_id", "subject_name"]

    def get_subject_id(self, obj):
        try:
            return str(obj.subject_id) if obj.subject_id else None
        except Exception:
            return None

    def get_subject_name(self, obj):
        try:
            return obj.subject.name if obj.subject_id else ""
        except Exception:
            return ""

    def get_teacher(self, obj):
        try:
            return obj.created_by.email if obj.created_by_id else ""
        except Exception:
            return ""


class DashboardPrivateSessionSerializer(serializers.ModelSerializer):
    student = serializers.SerializerMethodField()
    teacher_name = serializers.SerializerMethodField()
    date = serializers.DateField(source="scheduled_date")
    time = serializers.TimeField(source="scheduled_time")

    class Meta:
        model = PrivateSession
        fields = [
            "id", "subject", "student", "teacher_name", "date", "time",
            "duration_minutes", "status", "session_type",
        ]

    def get_student(self, obj):
        try:
            return obj.requested_by.email if obj.requested_by_id else ""
        except Exception:
            return ""

    def get_teacher_name(self, obj):
        try:
            return obj.teacher.email if obj.teacher_id else ""
        except Exception:
            return ""


class DashboardActivitySerializer(serializers.ModelSerializer):
    # Use direct fields — no content_object traversal
    subject_id = serializers.UUIDField(read_only=True)
    subject_name = serializers.CharField(read_only=True)
    object_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = [
            "id", "type", "title", "due_date", "created_at",
            "subject_id", "subject_name", "object_id", "is_read",
        ]
