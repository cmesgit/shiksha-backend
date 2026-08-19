from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404

from .models_recordings import SessionRecording, RecordingNote
from .serializers_recordings import SessionRecordingSerializer, RecordingNoteSerializer
from .models import Subject, Batch
from .services import teaches_subject
from accounts.permissions import IsTeacherContext, CTX_TEACHER
from config.bunny_signing import bunny_tus_ticket


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
            .filter(
                subject__teaching_assignments__teacher=request.user,
                subject__teaching_assignments__is_active=True,
            )
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
        # Both siblings below gate on this; only create was missing it, which
        # let any teacher-context account (including an auto-approved skill
        # expert, who is never reviewed) attach a recording to ANY subject —
        # it then renders to that course's enrolled students.
        if not teaches_subject(request.user, subject):
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
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

        # Deleting only the DB row orphans the Bunny Stream video — it keeps
        # billing forever with nothing in this app pointing at it. Bunny's
        # delete is best-effort: a network hiccup or an already-gone video
        # must never block removing the (broken/duplicate/wrong) DB row the
        # teacher is actually trying to clear.
        if recording.bunny_video_id:
            url = (
                f"https://video.bunnycdn.com/library/"
                f"{settings.BUNNY_LIBRARY_ID}/videos/{recording.bunny_video_id}"
            )
            try:
                requests.delete(
                    url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=10,
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Bunny video delete failed for %s: %s",
                    recording.bunny_video_id, e,
                )

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
        r = requests.post(url, json={"title": title}, headers=headers, timeout=(5, 30))
        if r.status_code not in [200, 201]:
            return Response({"error": r.text}, status=500)
        return Response({"video_id": r.json()["guid"]})


class SignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id required"}, status=400)

        return Response(bunny_tus_ticket(video_id))


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

        # live_session_id was accepted by the frontend but silently dropped
        # here — the model's own FK comment says this link is what lets the
        # admin console show a recording in the context of its source
        # session (and is the prerequisite for any future egress
        # automation). When given, it also backfills batch/session_date so
        # a teacher recording a real class doesn't have to re-enter what
        # the LiveSession already knows.
        live_session = None
        live_session_id = request.data.get("live_session_id")
        if live_session_id:
            from livestream.models import LiveSession
            live_session = get_object_or_404(
                LiveSession, id=live_session_id, subject=subject,
            )

        # Optional — NULL means course-wide (every batch of the subject sees
        # it). Explicit batch_id wins; otherwise inherit the source live
        # session's batch when there is one.
        batch = None
        batch_id = request.data.get("batch_id")
        if batch_id:
            batch = get_object_or_404(Batch, id=batch_id)
        elif live_session:
            batch = live_session.batch

        if not session_date and live_session:
            session_date = live_session.start_time.date()

        recording = SessionRecording.objects.create(
            subject=subject,
            batch=batch,
            live_session=live_session,
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
                url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=(5, 30))
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


def _require_recording_viewer(request, recording):
    """Same shape as livestream's _require_session_participant: teacher
    context needs to teach the recording's subject; otherwise the user needs
    an active enrollment in its course. Raises PermissionDenied (403) rather
    than returning a Response, so call sites can use it as a one-line guard.
    """
    from rest_framework.exceptions import PermissionDenied
    from enrollments.models import Enrollment

    user = request.user
    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )

    if in_teacher_context:
        if not teaches_subject(user, recording.subject):
            raise PermissionDenied("Not assigned to this subject.")
    else:
        if not Enrollment.objects.filter(
            user=user, course=recording.subject.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists():
            raise PermissionDenied("Not enrolled in this course.")


class RecordingNotesView(APIView):
    """GET/PATCH recordings/<id>/notes/ — the requesting user's own private
    notes on this recording (never another viewer's). PATCH upserts via
    update_or_create, same autosave-friendly shape as the live session's own
    SessionNote this mirrors.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject"), id=recording_id
        )
        _require_recording_viewer(request, recording)

        note = RecordingNote.objects.filter(recording=recording, user=request.user).first()
        return Response(
            RecordingNoteSerializer(note).data if note else {"content": "", "updated_at": None}
        )

    def patch(self, request, recording_id):
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject"), id=recording_id
        )
        _require_recording_viewer(request, recording)

        serializer = RecordingNoteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        note, _ = RecordingNote.objects.update_or_create(
            recording=recording,
            user=request.user,
            defaults=serializer.validated_data,
        )
        return Response(RecordingNoteSerializer(note).data)
