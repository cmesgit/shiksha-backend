from rest_framework import serializers
from .models_recordings import SessionRecording


class SessionRecordingSerializer(serializers.ModelSerializer):

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
            "thumbnail_url",
            "created_at",
            "is_published"
        ]
