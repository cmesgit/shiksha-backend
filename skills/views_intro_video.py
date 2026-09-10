# PLACEMENT: skills/views_intro_video.py
# Bunny.net-backed upload flow for an expert's single profile intro video —
# an advertising clip, not a session recording. Mirrors the Academy pattern in
# courses/views_recordings.py (CreateVideoSlotView/SignedUploadUrlView/
# CheckVideoStatusView) but scoped to the caller's own ExpertProfile via
# _get_expert(), since there is exactly one video per expert (no subject_id /
# recording_id path params needed).
#
# The Bunny round trip and the duration limit both live in skills/intro_video.py,
# shared with the per-listing flow in listing_intro_video.py.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import IsTeacher
from .teacher_views import _get_expert
from . import profile_ops
from .models import PendingIntroVideoUpload
from .intro_video import (
    BunnyUnavailable,
    attach,
    create_bunny_video,
    needs_sync,
    restore,
    snapshot,
    sync_intro_video,
)
from config.bunny_signing import bunny_tus_ticket


class CreateIntroVideoSlotView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        title = request.data.get("title") or "expert-intro-video"
        try:
            video_id = create_bunny_video(title)
        except BunnyUnavailable as e:
            return Response({"error": str(e)}, status=502)
        PendingIntroVideoUpload.objects.create(video_id=video_id, created_by=request.user)
        return Response({"video_id": video_id})


class IntroVideoSignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id required"}, status=400)

        # Previously signed a valid TUS upload ticket for ANY client-supplied
        # video_id with no ownership check at all. Must be either a slot THIS
        # caller just created, or the video_id already attached to their own
        # ExpertProfile (re-upload/replace).
        owns_pending = PendingIntroVideoUpload.objects.filter(
            video_id=video_id, created_by=request.user
        ).exists()
        ep = _get_expert(request.user)
        owns_current = bool(ep and ep.intro_video_bunny_id == video_id)
        if not (owns_pending or owns_current):
            return Response({"error": "Not allowed."}, status=403)

        return Response(bunny_tus_ticket(video_id))


class SaveIntroVideoView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id is required."}, status=400)

        ep = _get_expert(request.user)
        previous = snapshot(ep)
        attach(ep, video_id)

        # Ask Bunny what actually happened rather than assuming "Uploaded".
        # This is also where an over-length clip is caught for callers that
        # skipped the browser's own check.
        _, error = sync_intro_video(ep)
        if error:
            # A rejected replacement is a no-op — put the previous clip back
            # rather than leaving the profile holding a failed one.
            restore(ep, previous)
            return Response({"error": error}, status=400)

        # Bunny may still be transcoding; the status endpoint finishes the job.
        if ep.intro_video_status is None:
            ep.intro_video_status = 1  # Uploaded
            ep.save(update_fields=["intro_video_status", "updated_at"])

        PendingIntroVideoUpload.objects.filter(video_id=video_id).delete()
        return Response(profile_ops.serialize_expert(ep))


class IntroVideoStatusView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def get(self, request):
        ep = _get_expert(request.user)
        if needs_sync(ep):
            sync_intro_video(ep)
        return Response(profile_ops.serialize_expert(ep))
