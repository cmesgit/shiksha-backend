from activity.models import Activity
from quizzes.models import Quiz
from assignments.models import Assignment
from rest_framework import serializers
from livestream.models import LiveSession
from sessions_app.models import PrivateSession


class DashboardSessionSerializer(serializers.ModelSerializer):

    subject = serializers.CharField(source="subject.name")
    subject_id = serializers.IntegerField(source="subject.id", read_only=True)
    topic = serializers.CharField(source="title")
    teacher = serializers.CharField(source="created_by.email")
    dateTime = serializers.DateTimeField(source="start_time")

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "subject",
            "subject_id",
            "topic",
            "teacher",
            "dateTime"
        ]


class DashboardAssignmentSerializer(serializers.ModelSerializer):

    teacher = serializers.SerializerMethodField()
    due = serializers.DateTimeField(source="due_date")
    subject_id = serializers.IntegerField(source="chapter.subject.id", read_only=True)
    subject_name = serializers.CharField(source="chapter.subject.name", read_only=True)

    class Meta:
        model = Assignment
        fields = [
            "id",
            "title",
            "teacher",
            "due",
            "subject_id",
            "subject_name",
        ]

    def get_teacher(self, obj):

        teacher = obj.chapter.subject.subject_teachers.first()

        if teacher:
            return teacher.teacher.email

        return "Unknown"


class DashboardQuizSerializer(serializers.ModelSerializer):

    teacher = serializers.CharField(source="created_by.email")
    due = serializers.DateTimeField(source="due_date")
    subject_id = serializers.IntegerField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "teacher",
            "due",
            "subject_id",
            "subject_name",
        ]


class DashboardPrivateSessionSerializer(serializers.ModelSerializer):

    student = serializers.CharField(source="requested_by.email")
    teacher_name = serializers.CharField(source="teacher.email")
    date = serializers.DateField(source="scheduled_date")
    time = serializers.TimeField(source="scheduled_time")

    class Meta:
        model = PrivateSession
        fields = [
            "id",
            "subject",
            "student",
            "teacher_name",
            "date",
            "time",
            "duration_minutes",
            "status",
            "session_type",
        ]


class DashboardActivitySerializer(serializers.ModelSerializer):

    subject_id = serializers.SerializerMethodField()
    object_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "title",
            "due_date",
            "created_at",
            "subject_id",
            "object_id",
        ]

    def get_subject_id(self, obj):
        try:
            linked = obj.content_object
            if obj.type == "ASSIGNMENT" and linked:
                return linked.chapter.subject.id
            if obj.type == "QUIZ" and linked:
                return linked.subject.id
            if obj.type == "SESSION" and linked:
                return linked.subject.id
        except Exception:
            pass
        return None
