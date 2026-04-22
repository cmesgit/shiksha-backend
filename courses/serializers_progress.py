from rest_framework import serializers
from .models_progress import VideoProgress


class VideoProgressSerializer(serializers.ModelSerializer):

    percent_complete = serializers.SerializerMethodField()   # ← added
    duration_seconds = serializers.SerializerMethodField()   # ← added

    class Meta:
        model = VideoProgress
        fields = [
            "recording",
            "last_position",
            "completed",
            "percent_complete",
            "duration_seconds",
            "last_watched_at",
        ]

    def get_percent_complete(self, obj):
        duration = obj.recording.duration_seconds
        if not duration or duration == 0:
            return None
        return round((obj.last_position / duration) * 100, 1)

    def get_duration_seconds(self, obj):
        return obj.recording.duration_seconds
