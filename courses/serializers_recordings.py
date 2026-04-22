from rest_framework import serializers
from .models_recordings import SessionRecording


class SessionRecordingSerializer(serializers.ModelSerializer):

    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "subject",
            "chapter",
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

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        if not user:
            return None
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            return profile.full_name
        return user.get_full_name() or user.username
