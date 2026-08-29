import json

from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404

from .models_recordings import SessionRecording, RecordingNote, PendingVideoUpload
from .serializers_recordings import (
    RecordingNoteSerializer,
    SessionRecordingSerializer,
    SessionRecordingUpdateSerializer,
)
from .models import Subject, Batch
from .chapter_tags import (
    primary_chapter,
    resolve_tags,
    set_tags,
    validate_tag_payload,
)
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


def _as_bool(raw):
    """Coerce a chapter-tag flag to a bool.

    This endpoint is JSON, so a real `true`/`false` arrives as a bool and
    passes straight through. The string branch is for the multipart dialect
    the same keys travel in on the material upload — accepting both here
    means a client that already speaks one cannot be caught out by the other.
    Note `str(False).lower()` is "false", which is NOT in the truthy set, so
    the classic "the string 'false' is truthy" bug cannot appear.
    """
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _chapter_tags_from(data):
    """Read `chapter_tags` out of a request body.

    A JSON list of objects (what this endpoint's clients send), or the
    JSON-encoded *string* form multipart clients are forced into. Anything
    that is not a list of dicts is treated as "no tags": tags are optional on
    every surface, so the safe failure is to ignore them rather than 500.

    Deliberately duplicated from materials/views.py rather than imported.
    `materials` already depends on `courses`; importing back the other way
    would make the two apps mutually dependent for eight lines of parsing.
    """
    raw = data.get("chapter_tags")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        candidates = parsed if isinstance(parsed, list) else [parsed]
    return [c for c in candidates if isinstance(c, dict)]


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


# CreateRecordingView used to live here, routed at
# `subjects/<id>/recordings/create/`. It took a raw SessionRecordingSerializer
# payload — a ModelSerializer with no read_only_fields — so a caller supplied
# their own `bunny_video_id`, `status` and `is_published` directly. That
# bypassed the whole PendingVideoUpload ownership scheme
# (CreateVideoSlotView → SignedUploadUrlView) whose entire job is proving the
# caller owns the Bunny slot they are about to attach.
#
# Deleted rather than locked down: grepping all five frontends
# (teacher/student/admin dashboards, shiksha-frontend, shikshacom_app) for
# `recordings/create/` returned zero hits. The only live create path is
# SaveRecordingView, which validates ownership properly.


class DeleteRecordingView(APIView):
    # NOT IsTeacherContext. That permission class requires has_role("TEACHER")
    # AND a teacher JWT context claim, so a pure admin was rejected at the
    # class gate and the `request.user.is_staff` branch that used to be in the
    # body below was unreachable dead code — admin delete parity was blocked by
    # the decorator, not the logic. Authorization is now _require_recording_editor,
    # which PATCH shares, so the two can never drift the way recordings and
    # materials once did.
    permission_classes = [IsAuthenticated]

    def delete(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)
        _require_recording_editor(request, recording)

        bunny_video_id = recording.bunny_video_id

        # Order matters. The Bunny delete used to run FIRST, so a subsequent
        # DB failure left a live row pointing at a video that no longer
        # existed — an unplayable recording nobody could tell was broken.
        # Now the row goes first, inside a transaction, and Bunny is only told
        # once that commit succeeds.
        with transaction.atomic():
            # ContentChapterTag is a GENERIC relation keyed on a plain UUID
            # (models_chapter_tags.py: `object_id = UUIDField(db_index=True)`),
            # so there is no FK and nothing cascades. Deleting a recording
            # without this leaves its tag rows behind forever.
            set_tags(recording, [])
            recording.delete()

            if bunny_video_id:
                # Deleting only the DB row orphans the Bunny Stream video — it
                # keeps billing forever with nothing in this app pointing at
                # it. Bunny's delete stays best-effort: a network hiccup or an
                # already-gone video must never block removing the
                # (broken/duplicate/wrong) DB row the teacher is actually
                # trying to clear.
                transaction.on_commit(
                    lambda: _delete_bunny_video(bunny_video_id)
                )

        return Response(status=status.HTTP_204_NO_CONTENT)


def _delete_bunny_video(bunny_video_id):
    """Best-effort DELETE of a Bunny Stream video. Never raises."""
    url = (
        f"https://video.bunnycdn.com/library/"
        f"{settings.BUNNY_LIBRARY_ID}/videos/{bunny_video_id}"
    )
    try:
        # timeout=(5, 30) to match every other Bunny call in this file; it was
        # a bare 10 here, which is a shorter read budget than the sibling
        # calls for no stated reason.
        requests.delete(
            url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=(5, 30),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Bunny video delete failed for %s: %s", bunny_video_id, e,
        )


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
        # it). An EXPLICIT batch_id wins; a batch_id the caller did not
        # mention at all inherits the source live session's batch.
        #
        # PRESENCE, not truthiness. This used to be `if batch_id:` with a bare
        # `elif live_session:` fallback, which cannot tell "the teacher
        # deliberately chose All batches" (batch_id=null) from "the field was
        # never sent". Uploading from a Live Session detail page therefore
        # silently overrode an explicit All-batches choice with that session's
        # own batch — so a recording the teacher meant for the whole course
        # reached one batch, half the students never saw it, and nothing
        # anywhere reported a problem. Reproduced live before fixing; see
        # RecordingLiveSessionBatchOverrideTest.
        batch = None
        batch_supplied = "batch_id" in request.data
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
        elif live_session and not batch_supplied:
            batch = live_session.batch

        if not session_date and live_session:
            session_date = live_session.start_time.date()

        # Chapter placement. `SessionRecording` has carried `chapter`,
        # `chapter_tags`, `chapter_note` and `no_specific_chapter` since the
        # tagging system landed, and `SessionRecordingSerializer` returns all
        # four — but this view read none of them, so an upload could only ever
        # produce an untagged recording. The same contract as the material
        # upload and `SessionRecordingUpdateSerializer`: pick any number of
        # syllabus chapters, type your own, or say "no specific chapter" —
        # none of it required.
        #
        # Resolved BEFORE the row is written so a contradictory payload 400s
        # instead of leaving a half-placed recording behind, and AFTER the
        # batch lookup above so a foreign-course batch 404 cannot first mint
        # stray Chapter rows via save_chapters_to_course.
        raw_tags = _chapter_tags_from(request.data)
        no_specific = _as_bool(request.data.get("no_specific_chapter"))
        try:
            validate_tag_payload(raw_tags, no_specific)
            resolved_tags = resolve_tags(
                subject, raw_tags, teacher=request.user,
                # OFF unless asked. A teacher's own shorthand stays private
                # free text by default — the legacy `custom_chapter` key on
                # the material upload promoted every typed name into the
                # shared syllabus with no way to decline, and this endpoint
                # must not repeat that.
                save_to_course=_as_bool(
                    request.data.get("save_chapters_to_course")
                ),
            )
        except DRFValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

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
            # The additive invariant — see chapter_tags.primary_chapter().
            # The single FK keeps pointing at the first resolved chapter so
            # every legacy chapter-filtered read still finds the recording.
            chapter=primary_chapter(resolved_tags),
            chapter_note=(request.data.get("chapter_note") or ""),
            no_specific_chapter=no_specific,
        )
        set_tags(recording, resolved_tags)
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
        _notify_recording_published(recording)

        return Response(SessionRecordingSerializer(recording).data)


def _notify_recording_published(recording):
    """Tell the students who can actually SEE this recording that it exists.

    Extracted so the upload path (SaveRecordingView) and the publish path
    (RecordingDetailView.patch, on the False→True edge) cannot drift — the
    batch-visibility rule below is the whole point, and two copies of it would
    eventually disagree about who gets told.

    _enrollments_for applies the same batch-visibility rule the READER applies,
    so a batch-scoped recording never notifies a batch that would 403 on it;
    _bulk_notify_students writes the durable Activity + Notification rows plus
    one WS frame per row carrying the serialized activity, so dedupe and
    mark-read work against /activity/feed/.

    Never raises.
    """
    subject = recording.subject
    if subject is None:
        # Group-session recordings have no subject and therefore no enrolment
        # set to notify. Their audience is the people who attended.
        return
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

    def patch(self, request, recording_id):
        """Edit a recording's metadata. PARTIAL updates only.

        There was no update endpoint of any kind before this: RecordingDetailView
        was GET-only, so renaming a recording, retagging its chapter, moving it
        between batches or unpublishing it all required Django admin.

        PUT is deliberately not implemented (405). The edit modal sends only
        what changed, and a full replace would need every writable field on
        every save — which is how a client ends up blanking a description it
        never showed the teacher.
        """
        recording = get_object_or_404(
            SessionRecording.objects.select_related("subject__course__board"),
            id=recording_id,
        )
        _require_recording_editor(request, recording)

        was_published = recording.is_published

        serializer = SessionRecordingUpdateSerializer(
            recording, data=request.data, partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()

        # Only the False→True EDGE notifies. Re-saving an already-published
        # recording is silent, which is what makes a title fix cheap.
        #
        # Known and accepted: unpublish-then-republish notifies twice.
        # Assignments behave the same way today (see
        # TeacherAssignmentUpdateSerializer's is_published comment); deduping
        # would need a `notified_at` column, which is its own decision.
        if not was_published and updated.is_published:
            _notify_recording_published(updated)

        return Response(SessionRecordingSerializer(updated).data)


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

    Two more, added with the recording PATCH endpoint:

    · STAFF WERE DENIED. _authorize_subject_recordings (the list twin, above)
      has an `is_staff` branch; this one didn't, so an admin who was not also
      in teacher context fell through to the learner branch and was rejected
      for having no learner profile. That made every per-id endpoint —
      including /playback/ — unusable from the admin console.
    · is_published WAS NEVER CHECKED. Harmless while only Django admin could
      unpublish anything; the moment PATCH made unpublish a real teacher
      action it became live: a teacher hiding a wrong recording would
      reasonably expect it to stop being reachable, and it didn't.
    """
    from rest_framework.exceptions import PermissionDenied
    from accounts.auth_flow import get_active_profile
    from enrollments.models import Enrollment
    from enrollments.services import active_batch_id

    user = request.user
    if user.is_staff:
        return

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

    # Checked LAST, deliberately: an unpublished recording should read as
    # "not there yet" to someone who is otherwise entitled to the course, and
    # as "not yours" to someone who isn't. Running it before the enrolment
    # checks would tell an outsider which ids exist.
    if not recording.is_published:
        raise PermissionDenied("This recording isn't published yet.")


def _require_recording_editor(request, recording):
    """Who may MUTATE a recording — PATCH and DELETE share this.

    Deliberately one helper rather than a check inlined in each view. The
    delete rules for recordings and study materials drifted apart once already
    (one allowed any co-teacher, the other only the uploader) even though both
    list endpoints return colleagues' content, so on one screen the delete
    button worked and on the other it always 403'd. Two call sites reading the
    same function cannot do that.

    Staff, or teacher context assigned to the recording's subject. Note this
    is NOT the same as _require_recording_viewer: an enrolled learner can watch
    a recording and must never be able to rename or delete it.
    """
    from rest_framework.exceptions import PermissionDenied

    user = request.user
    if user.is_staff:
        return

    token = getattr(request, "auth", None)
    in_teacher_context = (
        bool(token) and token.get("context") == CTX_TEACHER and user.has_role("TEACHER")
    )
    if in_teacher_context and teaches_subject(user, recording.subject):
        return

    raise PermissionDenied("You cannot modify this recording.")


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

        # Player options the client used to append itself. The seek parameter
        # is NOT part of the signature, so a viewer nudging it cannot
        # invalidate (or extend) the token.
        #
        # THE PARAMETER IS `t`, NOT `start`. Bunny documents `t` (accepting
        # `30s`, `1h20m45s`, `hh:mm:ss` or a bare number of seconds); `start`
        # is not a parameter it recognises, so the player silently ignored it.
        # This code and all three frontends sent `start` — which means
        # "resume where you left off" has never once resumed, on any screen,
        # since the feature was written. See
        # https://bunny.net/docs/stream-embedding-videos
        params = {"autoplay": "false"}

        # Default to the trim point, so a trimmed recording opens at its real
        # beginning even when the caller passes nothing.
        seek = recording.clamp_position(
            request.query_params.get("start") or recording.effective_start_seconds
        )
        if seek > 0:
            params["t"] = int(seek)

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
            # The FULL Bunny length — CheckVideoStatusView owns this field.
            "duration_seconds": recording.duration_seconds,
            # The trimmed window, resolved here so clients don't each
            # reimplement it.
            "trim_start_seconds": recording.trim_start_seconds,
            "trim_end_seconds": recording.trim_end_seconds,
            "effective_duration_seconds": recording.effective_duration_seconds,
            "start": int(seek),
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
