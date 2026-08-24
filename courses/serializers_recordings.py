from rest_framework import serializers

from .board_display import board_name_via
from .models_recordings import SessionRecording, RecordingNote
from .chapter_tags import serialize_tags


class SessionRecordingSerializer(serializers.ModelSerializer):

    uploaded_by_name = serializers.SerializerMethodField()
    # The flat faculty Recordings grid spans subjects, so a card has to
    # name its own. `subject` above is the raw FK id.
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.SerializerMethodField()
    board_name = serializers.SerializerMethodField()
    # Multi-chapter placement; `chapter` above stays the single-value view.
    chapter_tags = serializers.SerializerMethodField()

    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "subject",
            "subject_name",
            "course_title",
            "board_name",
            "chapter",
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "batch",
            "live_session",
            "title",
            "description",
            "session_date",
            "duration_seconds",
            "bunny_video_id",
            "status",
            "thumbnail_url",
            "created_at",
            "is_published",
            "uploaded_by_name",
        ]

    def get_course_title(self, obj):
        course = getattr(obj.subject, "course", None) if obj.subject_id else None
        return course.title if course else None

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        if not user:
            return None
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            return profile.full_name
        return user.get_full_name() or user.username


class RecordingNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecordingNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]
