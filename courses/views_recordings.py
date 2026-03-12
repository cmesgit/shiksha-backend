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

        recording.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
