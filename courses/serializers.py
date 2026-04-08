from .models_recordings import SessionRecording
from .models import Chapter
from rest_framework import serializers
from .models import Subject, Course, Board


class SubjectSerializer(serializers.ModelSerializer):
    teachers = serializers.SerializerMethodField()
    chapters = serializers.SerializerMethodField()   # ✅ added
    serializer = SubjectSerializer(subjects, many=True, context={'request': request})
    stream_name = serializers.CharField(
        source="course.stream.name", read_only=True)
    board = serializers.SerializerMethodField()

    class Meta:
        model = Subject
        fields = (
            "id",
            "name",
            "order",
            "image",
            "teachers",
            "chapters",   # ✅ added
            "stream_name",
            "board",
        )
    def get_image(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

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
                "name": getattr(teacher, 'profile', None) and teacher.profile.full_name or teacher.username,
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

    def get_board(self, obj):
        if not obj.course or not obj.course.board:
            return None
        return {
            "id": str(obj.course.board.id),
            "name": obj.course.board.name,
            "board_type": obj.course.board.board_type,
        }


class BoardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Board
        fields = ("id", "name", "board_type")


class CourseSerializer(serializers.ModelSerializer):
    board = BoardSerializer(read_only=True)
    stream_name = serializers.CharField(source="stream.name", read_only=True)

    class Meta:
        model = Course
        fields = (
            "id",
            "title",
            "description",
            "stream_name",
            "board",
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
