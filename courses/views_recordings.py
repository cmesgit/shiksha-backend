from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404

from .models_recordings import SessionRecording
from .serializers_recordings import SessionRecordingSerializer
from .models import Subject, Batch
from .services import teaches_subject
from accounts.permissions import IsTeacherContext


class SubjectRecordingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        from django.db.models import Q
        from accounts.auth_flow import get_active_profile
        from enrollments.services import active_batch_id
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)
        recordings = SessionRecording.objects.filter(
            subject=subject,
            is_published=True
        )
        # Teachers/staff see every batch's recordings; a student sees only
        # course-wide (batch IS NULL) recordings plus their own batch's.
        # Scoped to the ACTIVE PROFILE, not the account — see active_batch_id.
        if not (request.user.is_staff or teaches_subject(request.user, subject)):
            batch_id = active_batch_id(
                learner_profile=get_active_profile(request),
                course_id=subject.course_id,
            )
            recordings = recordings.filter(
                Q(batch__isnull=True) | Q(batch_id=batch_id))
        serializer = SessionRecordingSerializer(recordings, many=True)
        return Response(serializer.data)


class TeacherAllRecordingsView(APIView):
    """Every recording across every subject this teacher is assigned to.

    The faculty Recordings screen is one flat, subject-filtered grid (design
    handoff screen 9). Before this existed the frontend called
    SubjectRecordingsView once per subject and flattened client-side.

    No batch filter and no is_published filter — SubjectRecordingsView already
    gives teachers every batch's recordings, and a teacher needs to see their
    own unpublished/still-processing uploads (the grid shows those as Pending).
    """

    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request):
        recordings = (
            SessionRecording.objects
            .filter(subject__subject_teachers__teacher=request.user)
            .select_related("subject")
            # distinct(): a teacher listed twice on one subject would otherwise
            # duplicate every recording on it.
            .distinct()
        )
        serializer = SessionRecordingSerializer(recordings, many=True)
        return Response(serializer.data)


class CreateRecordingView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        serializer = SessionRecordingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(subject=subject, uploaded_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DeleteRecordingView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def delete(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)
        if not teaches_subject(request.user, recording.subject):
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        recording.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CreateVideoSlotView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request):
        title = request.data.get("title")
        url = f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos"
        headers = {
            "AccessKey": settings.BUNNY_API_KEY,
            "Content-Type": "application/json"
        }
        r = requests.post(url, json={"title": title}, headers=headers)
        if r.status_code not in [200, 201]:
            return Response({"error": r.text}, status=500)
        return Response({"video_id": r.json()["guid"]})


class SignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id required"}, status=400)

        return Response({
            "upload_url": (
                f"https://video.bunnycdn.com/library/"
                f"{settings.BUNNY_LIBRARY_ID}/videos/{video_id}"
            ),
            "access_key": settings.BUNNY_API_KEY,
        })


class SaveRecordingView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)

        if not teaches_subject(request.user, subject):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN
            )

        title = request.data.get("title")
        session_date = request.data.get("session_date")
        video_id = request.data.get("video_id")
        description = request.data.get("description", "")

        if not video_id:
            return Response({"error": "video_id is required."}, status=400)

        # Optional — NULL means course-wide (every batch of the subject sees
        # it), the model's own default for a manual upload with no source
        # LiveSession to inherit scope from.
        batch = None
        batch_id = request.data.get("batch_id")
        if batch_id:
            batch = get_object_or_404(Batch, id=batch_id)

        recording = SessionRecording.objects.create(
            subject=subject,
            batch=batch,
            title=title,
            description=description,
            session_date=session_date or None,
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

        try:
            r = requests.get(
                url, headers={"AccessKey": settings.BUNNY_API_KEY})
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

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Bunny status check failed: %s", e)

        return Response(SessionRecordingSerializer(recording).data)


class RecordingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)
        return Response(SessionRecordingSerializer(recording).data)
