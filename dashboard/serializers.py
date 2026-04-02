from activity.models import Activity
from quizzes.models import Quiz
from assignments.models import Assignment
from rest_framework import serializers
from livestream.models import LiveSession


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


class DashboardActivitySerializer(serializers.ModelSerializer):

    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "title",
            "due_date",
            "created_at"
        ]
