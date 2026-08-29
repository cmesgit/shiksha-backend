from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from .models_progress import VideoProgress
from .models_recordings import SessionRecording
from .views_recordings import _require_recording_viewer


def _progress_key(request):
    """The (student, learner_profile) pair a VideoProgress row is keyed on.

    Rows used to be keyed on `student=request.user` — the ACCOUNT — so two
    siblings on one parent account shared a single watch position: whoever
    watched last moved the other's resume point, and "Watched" on one child's
    card meant nothing about the other. Same account-vs-profile confusion as
    rosters and attendance (audit theme T2).

    A teacher-context viewer has no learner profile, so their rows stay
    account-keyed with learner_profile NULL; the partial unique constraints on
    the model keep the two shapes from colliding.
    """
    from accounts.auth_flow import get_active_profile
    return request.user, get_active_profile(request)


class GetVideoProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)
        # Guarded. Every sibling per-id recording view calls this; these two
        # progress views were the only ones that didn't, so any authenticated
        # account could read back a recording's duration_seconds — and, via
        # the POST below, write a progress row — against ANY recording UUID,
        # with no enrolment, batch or teaching check at all.
        _require_recording_viewer(request, recording)

        student, learner_profile = _progress_key(request)
        progress = VideoProgress.objects.filter(
            student=student,
            learner_profile=learner_profile,
            recording=recording
        ).first()

        # The trimmed window, resolved server-side. Clients render these rather
        # than recomputing: three apps each doing their own trim arithmetic is
        # three chances to disagree, and Admin cannot share code with the
        # teacher/student apps anyway.
        trim = {
            "trim_start_seconds": recording.trim_start_seconds,
            "trim_end_seconds": recording.trim_end_seconds,
            "effective_duration_seconds": recording.effective_duration_seconds,
            # Still the FULL Bunny length. CheckVideoStatusView owns this field
            # and writes Bunny's `length` into it; renaming its meaning here
            # would break that.
            "duration_seconds": recording.duration_seconds,
        }

        if not progress:
            return Response({
                "last_position": recording.effective_start_seconds,
                "completed": False,
                "percent_complete": None,
                **trim,
            })

        return Response({
            "last_position": recording.clamp_position(progress.last_position),
            "completed": progress.completed,
            # Derived from the effective window, so a recording trimmed AFTER
            # someone watched it reads 100% rather than 143%.
            "percent_complete": recording.percent_watched(progress.last_position),
            "last_watched_at": progress.last_watched_at,
            **trim,
        })


class SaveVideoProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, recording_id):
        recording = get_object_or_404(SessionRecording, id=recording_id)
        _require_recording_viewer(request, recording)

        last_position = request.data.get("last_position", 0)
        completed = request.data.get("completed", False)

        # Validate position
        try:
            last_position = float(last_position)
            if last_position < 0:
                last_position = 0
        except (TypeError, ValueError):
            last_position = 0

        # Never let a client claim a position past the end of the video — with
        # duration_seconds finally being populated, an inflated value would
        # otherwise read back as >100% watched. Clamped to the TRIMMED window,
        # not the raw length, so a trim can't be walked past by posting a
        # bigger number.
        last_position = recording.clamp_position(last_position)

        # Auto-mark complete within the last 10 seconds of the VISIBLE window.
        # Against the raw duration instead, a 45-minute video trimmed to end at
        # 40:00 could never be completed by anyone — the last 5 minutes they'd
        # have to reach are the ones the trim removes.
        effective_end = recording.effective_end_seconds
        if effective_end and not completed:
            if last_position >= effective_end - 10:
                completed = True

        student, learner_profile = _progress_key(request)
        progress, _ = VideoProgress.objects.get_or_create(
            student=student,
            learner_profile=learner_profile,
            recording=recording
        )

        # Only update if new position is further ahead (don't rewind progress)
        if last_position > progress.last_position or completed:
            progress.last_position = last_position
            progress.completed = completed
            progress.save()

        return Response({"status": "ok"})
