
from .models_recordings import SessionRecording
from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404

from .models_recordings import SessionRecording
from .serializers_recordings import SessionRecordingSerializer
from .models import Subject
from accounts.permissions import IsTeacher


class SubjectRecordingsView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):

        subject = get_object_or_404(Subject, id=subject_id)

        recordings = SessionRecording.objects.filter(
            subject=subject,
            is_published=True
        )

        serializer = SessionRecordingSerializer(recordings, many=True)

        return Response(serializer.data)


class CreateRecordingView(APIView):

    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request, subject_id):

        subject = get_object_or_404(Subject, id=subject_id)

        serializer = SessionRecordingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        serializer.save(
            subject=subject,
            uploaded_by=request.user
        )

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DeleteRecordingView(APIView):

    permission_classes = [IsAuthenticated, IsTeacher]

    def delete(self, request, recording_id):

        recording = get_object_or_404(SessionRecording, id=recording_id)

        if not recording.subject.subject_teachers.filter(
            teacher=request.user
        ).exists():
            return Response(
                {"detail": "Not allowed."},
                status=status.HTTP_403_FORBIDDEN
            )

        recording.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class CreateVideoSlotView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request):

        title = request.data.get("title")

        url = f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos"

        headers = {
            "AccessKey": settings.BUNNY_API_KEY,
            "Content-Type": "application/json"
        }

        payload = {
            "title": title
        }

        r = requests.post(url, json=payload, headers=headers)

        if r.status_code not in [200, 201]:
            return Response({"error": r.text}, status=500)

        data = r.json()

        return Response({
            "video_id": data["guid"]
        })


class SaveRecordingView(APIView):

    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request, subject_id):

        subject = get_object_or_404(Subject, id=subject_id)

        # 🔐 ensure teacher is assigned to subject
        if not subject.subject_teachers.filter(teacher=request.user).exists():
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN
            )

        title = request.data.get("title")
        session_date = request.data.get("session_date")
        timing = request.data.get("duration")
        video_id = request.data.get("video_id")

        duration_seconds = None

        if timing:
            try:
                start, end = timing.split("-")

                from datetime import datetime

                start_time = datetime.strptime(start.strip(), "%I:%M %p")
                end_time = datetime.strptime(end.strip(), "%I:%M %p")

                duration_seconds = int((end_time - start_time).total_seconds())

            except Exception:
                duration_seconds = None

        recording = SessionRecording.objects.create(
            subject=subject,
            title=title,
            session_date=session_date,
            duration_seconds=duration_seconds,
            bunny_video_id=video_id,
            uploaded_by=request.user,
            status=1,
        )

        return Response(SessionRecordingSerializer(recording).data)


class CheckVideoStatusView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):

        recording = get_object_or_404(SessionRecording, id=recording_id)

        if recording.status == 4:
            return Response(SessionRecordingSerializer(recording).data)

        url = (
            f"https://video.bunnycdn.com/library/"
            f"{settings.BUNNY_LIBRARY_ID}/videos/{recording.bunny_video_id}"
        )
        headers = {"AccessKey": settings.BUNNY_API_KEY}

        try:
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                new_status = data.get("status", 0)
                recording.status = new_status

                if new_status == 4 and not recording.thumbnail_url:
                    thumb_file = data.get("thumbnailFileName", "")
                    cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
                    if thumb_file and cdn_host:
                        recording.thumbnail_url = (
                            f"https://{cdn_host}/{recording.bunny_video_id}/{thumb_file}"
                        )

                recording.save(update_fields=["status", "thumbnail_url"])

        except Exception:
            pass

        return Response(SessionRecordingSerializer(recording).data)


class RecordingDetailView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):

        recording = get_object_or_404(
            SessionRecording,
            id=recording_id
        )

        serializer = SessionRecordingSerializer(recording)

        return Response(serializer.data)
