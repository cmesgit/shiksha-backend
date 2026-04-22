from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from .models_progress import VideoProgress
from .models_recordings import SessionRecording


class GetVideoProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)

        progress = VideoProgress.objects.filter(
            student=request.user,
            recording=recording
        ).first()

        if not progress:
            return Response({
                "last_position": 0,
                "completed": False,
                "percent_complete": None,
                "duration_seconds": recording.duration_seconds,
            })

        duration = recording.duration_seconds
        percent = None
        if duration and duration > 0:
            percent = round((progress.last_position / duration) * 100, 1)

        return Response({
            "last_position": progress.last_position,
            "completed": progress.completed,
            "percent_complete": percent,
            "duration_seconds": duration,
            "last_watched_at": progress.last_watched_at,
        })


class SaveVideoProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)

        last_position = request.data.get("last_position", 0)
        completed = request.data.get("completed", False)

        # Validate position
        try:
            last_position = float(last_position)
            if last_position < 0:
                last_position = 0
        except (TypeError, ValueError):
            last_position = 0

        # Auto-mark complete if within last 10 seconds of video
        if recording.duration_seconds and not completed:
            if last_position >= recording.duration_seconds - 10:
                completed = True

        progress, _ = VideoProgress.objects.get_or_create(
            student=request.user,
            recording=recording
        )

        # Only update if new position is further ahead (don't rewind progress)
        if last_position > progress.last_position or completed:
            progress.last_position = last_position
            progress.completed = completed
            progress.save()

        return Response({"status": "ok"})
