from .models_recordings import SessionRecording
from .models import Chapter
from rest_framework import serializers
from .models import Subject, Course


class SubjectSerializer(serializers.ModelSerializer):
    teachers = serializers.SerializerMethodField()
    chapters = serializers.SerializerMethodField()   # ✅ added

    class Meta:
        model = Subject
        fields = (
            "id",
            "name",
            "order",
            "teachers",
            "chapters",   # ✅ added
        )

    def get_teachers(self, obj):
        subject_teachers = (
            obj.subject_teachers
            .select_related("teacher__teacher_profile")
            .order_by("order")
        )

        data = []

        for st in subject_teachers:
            teacher = st.teacher
            profile = getattr(teacher, "teacher_profile", None)

            data.append({
                "id": teacher.id,
                "name": teacher.username,
                "display_role": st.display_role,
                "qualification": profile.qualification if profile else "",
                "bio": profile.bio if profile else "",
                "rating": profile.rating if profile else None,
                "photo": profile.photo.url if profile and profile.photo else None,
            })

        return data

    # ✅ NEW METHOD
    def get_chapters(self, obj):
        return [
            {
                "id": str(ch.id),
                "title": ch.title,
                "order": ch.order,
            }
            for ch in obj.chapters.all().order_by("order")
        ]


class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = (
            "id",
            "title",
            "description",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class ChapterSerializer(serializers.ModelSerializer):

    class Meta:
        model = Chapter
        fields = ["id", "title", "order"]


class RecordingSerializer(serializers.ModelSerializer):

    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "title",
            "subject",
            "chapter",
            "session_date",
            "duration_seconds",
            "bunny_video_id",
            "thumbnail_url",
            "created_at",
        ]
