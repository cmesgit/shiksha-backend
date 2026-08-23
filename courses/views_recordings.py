from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404

from .models_recordings import SessionRecording, RecordingNote, PendingVideoUpload
from .serializers_recordings import SessionRecordingSerializer, RecordingNoteSerializer
from .models import Subject, Batch
from .services import teaches_subject
from accounts.permissions import IsTeacherContext, CTX_TEACHER
from config.bunny_signing import (
    bunny_embed_url,
    bunny_tus_ticket,
    upload_expiry_for_size,
)


# Sentinel returned as the "batch_id" for a teacher/staff caller. A student's
# batch_id may genuinely be None (enrolled but not yet placed in a cohort →
# course-wide recordings only), so plain None cannot also mean "unrestricted"
# without one of the two degrading into the other. Same sentinel pattern, for
# the same reason, as materials/views.py's TEACHER_UNRESTRICTED.
TEACHER_UNRESTRICTED = object()


def _authorize_subject_recordings(request, subject):
    """Gate a subject's recording LIST. Returns (allowed, batch_id).

    Deliberately the subject-level twin of _require_recording_viewer (below),
    which gates the per-id endpoints: teacher-or-staff → every batch; a
    learner → an ACTIVE enrollment on the ACTIVE PROFILE, scoped to their own
    batch plus course-wide rows.

    Enrollment, not subscription, is the learner test — that is what
    _require_recording_viewer checks, and the list must not admit anyone the
    detail endpoint then 403s (nor hide rows it would serve).
    """
    from accounts.auth_flow import get_active_profile
    from enrollments.models import Enrollment
    from enrollments.services import active_batch_id

    user = request.user
    if user.is_staff or teaches_subject(user, subject):
        return True, TEACHER_UNRESTRICTED

    learner = get_active_profile(request)
    if learner is None:
        return False, None
    if not Enrollment.objects.filter(
        learner_profile=learner,
        course_id=subject.course_id,
        status=Enrollment.STATUS_ACTIVE,
    ).exists():
        return False, None

    return True, active_batch_id(
        learner_profile=learner, course_id=subject.course_id,
    )


class SubjectRecordingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        from django.db.models import Q
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)

        # ENTITLEMENT FIRST. This view previously had NO gate of any kind:
        # permission_classes was IsAuthenticated and the only branch was
        # "teacher-or-staff sees every batch, everyone else gets batch
        # filtering". But active_batch_id() returns None for a caller with no
        # enrolment, so the "everyone else" filter degraded to
        # Q(batch__isnull=True) — i.e. EVERY course-wide recording, including
        # each row's bunny_video_id (the playback handle). Any authenticated
        # account could enumerate subject UUIDs from the public catalog and
        # read the whole published library of any paid course. Same hole, same
        # cause, as the one StudentSubjectMaterials already documents.
        #
        # The gate is _require_recording_viewer's rule applied at subject
        # level, deliberately: a list that admitted people the per-id detail
        # endpoint denies (or vice versa) is exactly how this drifted.
        allowed, batch_id = _authorize_subject_recordings(request, subject)
        if not allowed:
            return Response(
                {"detail": "You do not have access to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        recordings = SessionRecording.objects.filter(
            subject=subject,
            is_published=True
        ).select_related("subject__course__board")
        # Teachers/staff see every batch's recordings; a student sees only
        # course-wide (batch IS NULL) recordings plus their own batch's.
        # Scoped to the ACTIVE PROFILE, not the account — see active_batch_id.
        if batch_id is not TEACHER_UNRESTRICTED:
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
            .select_related("subject__course__board")
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
        # Subject teaching staff (or an admin) may delete, not just the
        # uploader. Deliberately the SAME rule as DeleteStudyMaterial — the
        # two used to disagree (recordings: any co-teacher; materials:
        # uploaded_by only) even though both list endpoints return
        # colleagues' content, so on one screen the delete button worked and
        # on the other it always 403'd. See that view for the reasoning.
        if not (
            request.user.is_staff
            or teaches_subject(request.user, recording.subject)
        ):
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
        video_id = r.json()["guid"]
        PendingVideoUpload.objects.create(video_id=video_id, created_by=request.user)
        return Response({"video_id": video_id})


class SignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id required"}, status=400)

        # Previously signed a valid TUS upload ticket for ANY client-supplied
        # video_id with no ownership check at all — any teacher-context
        # account could overwrite another teacher's recording, since
        # bunny_video_id is already serialized back out elsewhere in this
        # app. Must be either a slot THIS caller just created, or a video_id
        # already attached to a recording they teach (re-upload/replace).
        owns_pending = PendingVideoUpload.objects.filter(
            video_id=video_id, created_by=request.user
        ).exists()
        owns_recording = SessionRecording.objects.filter(
            bunny_video_id=video_id,
        ).filter(subject__teaching_assignments__teacher=request.user,
                  subject__teaching_assignments__is_active=True).exists()
        if not (owns_pending or owns_recording):
            return Response({"error": "Not allowed."}, status=403)

        # One ticket covers the WHOLE transfer and Bunny rejects every chunk
        # after it expires, with no resume path on the client. A flat 1 h
        # ticket therefore lost any upload slower than 4 GB/hour outright —
        # and the form permits 4 GB. Size the ticket to the declared file
        # instead (capped in bunny_signing). file_size is a hint, not a
        # trusted value: it only lengthens an already-authorised ticket for a
        # video_id this caller was just verified to own.
        expiry = upload_expiry_for_size(request.data.get("file_size"))
        return Response(bunny_tus_ticket(video_id, expiry_seconds=expiry))


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
            # course_id in the lookup, not just the pk. Without it a batch
            # belonging to a DIFFERENT course was accepted silently, and the
            # student read path filters on
            # `batch__isnull=True | batch_id=<their batch>` — which can never
            # match a foreign course's batch. The recording then showed as
            # published on the teacher's grid while being invisible to every
            # student alive. Fail loudly at write time instead.
            batch = get_object_or_404(
                Batch, id=batch_id, course_id=subject.course_id,
            )
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
        # The recording itself is now the ownership record for this video_id
        # (checked via teaches_subject above) — the pending-slot bookkeeping
        # row has done its job.
        PendingVideoUpload.objects.filter(video_id=video_id).delete()

        # Notify the students who can actually SEE this recording.
        #
        # The upload form's rail promises "Students notified — Auto-alert once
        # live" (UploadRecording.jsx). That was a lie: this view sent nothing,
        # activity/signals.py had no recording hook, and grepping for one
        # returned nothing. No bell, no Activity row, no WS frame — recordings
        # were the only student-facing content lifecycle with no notification
        # at all.
        #
        # Reuses the exact path study materials already take: _enrollments_for
        # applies the same batch-visibility rule the reader applies (so a
        # batch-scoped recording never notifies a batch that would 403 on it),
        # and _bulk_notify_students writes the durable Activity + Notification
        # rows plus one WS frame per row carrying the SERIALIZED ACTIVITY, so
        # dedupe and mark-read work against /activity/feed/.
        #
        # Best-effort: a notification backend hiccup must not fail an upload
        # whose 3 GB Bunny transfer already succeeded — the recording row is
        # the thing the teacher cannot cheaply redo.
        try:
            from activity.models import Activity
            from activity.signals import _bulk_notify_students, _enrollments_for

            _bulk_notify_students(
                _enrollments_for(subject.course, recording.batch_id),
                recording,
                Activity.TYPE_RECORDING,
                f"New recording: {recording.title}",
                None,                   # recordings have no due date
                subject.id,
                subject.name,
                verb="recording.uploaded",
                link_url=f"/subjects/recordings/{subject.id}",
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Recording notification failed for %s: %s", recording.id, e,
            )

        return Response(SessionRecordingSerializer(recording).data)


class CheckVideoStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        # Same missing guard as RecordingDetailView — and worse, an unguarded
        # call here also spends a Bunny API request on an id the caller has no
        # right to, and returns the same full serializer payload.
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject__course__board"),
            id=recording_id,
        )
        _require_recording_viewer(request, recording)

        # Finished AND we already know how long it is → nothing left to ask
        # Bunny. The duration half of that condition is load-bearing: this
        # early return used to fire on status alone, which meant every
        # recording that reached READY before duration capture existed could
        # never acquire one, because no other code path fetches it either.
        if recording.status == 4 and recording.duration_seconds:
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

                # DURATION. This is the only place the app ever sees Bunny's
                # `length` (seconds), and it used to throw it away — nothing
                # in the codebase wrote duration_seconds, so it stayed NULL
                # forever. Downstream, that made
                # GetVideoProgressView.percent_complete permanently null (the
                # student's progress bar pinned at 0% and the card printed no
                # duration) and made SaveVideoProgressView's auto-complete
                # branch unreachable, because both are computed from it.
                length = data.get("length")
                try:
                    length = int(length)
                except (TypeError, ValueError):
                    length = 0
                if length > 0:
                    recording.duration_seconds = length

                if new_status == 4 and not recording.thumbnail_url:
                    thumb_file = data.get("thumbnailFileName", "")
                    cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
                    if thumb_file and cdn_host:
                        recording.thumbnail_url = (
                            f"https://{cdn_host}/{recording.bunny_video_id}/{thumb_file}"
                        )

                recording.save(
                    update_fields=["status", "thumbnail_url", "duration_seconds"]
                )

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Bunny status check failed: %s", e)

        return Response(SessionRecordingSerializer(recording).data)


class RecordingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        # Guarded. This used to fetch by pk with NO entitlement check of any
        # kind, so any authenticated account could read any recording row —
        # including bunny_video_id, which is the playback handle. The guard
        # it needed was already defined a few lines up and used by
        # RecordingNotesView; this view just never called it.
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject__course__board"),
            id=recording_id,
        )
        _require_recording_viewer(request, recording)
        return Response(SessionRecordingSerializer(recording).data)


def _require_recording_viewer(request, recording):
    """Teacher context must teach the recording's subject; a learner must be
    enrolled in its course AND entitled to its batch. Raises PermissionDenied
    (403) rather than returning a Response, so call sites can use it as a
    one-line guard.

    Two fixes here, both audit findings:

    · ENROLLMENT WAS ACCOUNT-KEYED (`user=user`). On a one-email/many-children
      account that let sibling A's enrolment authorise sibling B. Now keyed on
      the ACTIVE learner profile, like every other correct read path.
    · BATCH WAS NEVER CHECKED, even though SessionRecording.batch exists
      precisely so "the batch that attended the class sees its recording;
      other batches don't" (see the field's own comment). SubjectRecordingsView
      filters on it; this guard didn't, so the per-id endpoints were a side
      door around the list's own rule.
    """
    from rest_framework.exceptions import PermissionDenied
    from accounts.auth_flow import get_active_profile
    from enrollments.models import Enrollment
    from enrollments.services import active_batch_id

    user = request.user
    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )

    if in_teacher_context:
        if not teaches_subject(user, recording.subject):
            raise PermissionDenied("Not assigned to this subject.")
        return

    learner = get_active_profile(request)
    if learner is None:
        raise PermissionDenied("Select a learner profile.")

    course = recording.subject.course
    if not Enrollment.objects.filter(
        learner_profile=learner, course=course,
        status=Enrollment.STATUS_ACTIVE,
    ).exists():
        raise PermissionDenied("Not enrolled in this course.")

    # NULL batch = shared course-wide; otherwise it must be this learner's.
    if recording.batch_id is not None:
        if recording.batch_id != active_batch_id(
            learner_profile=learner, course_id=course.id,
        ):
            raise PermissionDenied("This recording is not available to your batch.")


class RecordingPlaybackView(APIView):
    """GET recordings/<id>/playback/ → a SHORT-LIVED, SIGNED iframe URL.

    Replaces the client building
    `https://iframe.mediadelivery.net/embed/{LIBRARY_ID}/{videoId}` itself
    from a library id shipped in the bundle. That URL was unauthenticated and
    permanent: copy it out of devtools, cancel the subscription, keep
    streaming — or post it publicly. Every check in
    _require_recording_viewer was bypassed by one copied string, forever.

    Now the entitlement check runs on EVERY playback, the URL expires (see
    bunny_signing.EMBED_EXPIRY_SECONDS), and neither the library id nor any
    Bunny key reaches the browser.

    `token_auth` in the response is the honest signal, not decoration: it is
    False when BUNNY_STREAM_TOKEN_KEY is unset, in which case the URL is the
    old permanent one and the caller should be under no illusion that
    playback is gated. See bunny_signing's module docstring for the two
    things ops must configure.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject__course"),
            id=recording_id,
        )
        _require_recording_viewer(request, recording)

        if not recording.bunny_video_id:
            return Response(
                {"detail": "This recording has no video attached."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Player options the client used to append itself. `start` resumes at
        # the saved watch position; it is NOT part of the signature, so a
        # viewer nudging it cannot invalidate (or extend) the token.
        params = {"autoplay": "false"}
        try:
            start = int(float(request.query_params.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        if start > 0:
            params["start"] = start

        url, expires, signed = bunny_embed_url(
            recording.bunny_video_id, params=params,
        )
        if not url:
            return Response(
                {
                    "detail": "Playback isn't configured on this server.",
                    "code": "playback_not_configured",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            "embed_url": url,
            "expires": expires,
            "token_auth": signed,
            "duration_seconds": recording.duration_seconds,
        })


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
