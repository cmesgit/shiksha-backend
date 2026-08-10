# PLACEMENT: skills/views_intro_video.py (new file)
# Bunny.net-backed upload flow for an expert's single profile intro video —
# an advertising clip, not a session recording. Mirrors the Academy pattern in
# courses/views_recordings.py (CreateVideoSlotView/SignedUploadUrlView/
# CheckVideoStatusView) but scoped to the caller's own ExpertProfile via
# _get_expert(), since there is exactly one video per expert (no subject_id /
# recording_id path params needed).
from django.conf import settings
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import IsTeacher
from .teacher_views import _get_expert
from . import profile_ops
from config.bunny_signing import bunny_tus_ticket


class CreateIntroVideoSlotView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        title = request.data.get("title") or "expert-intro-video"
        url = f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos"
        headers = {
            "AccessKey": settings.BUNNY_API_KEY,
            "Content-Type": "application/json"
        }
        r = requests.post(url, json={"title": title}, headers=headers, timeout=(5, 30))
        if r.status_code not in [200, 201]:
            return Response({"error": r.text}, status=500)
        return Response({"video_id": r.json()["guid"]})


class IntroVideoSignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id required"}, status=400)

        return Response(bunny_tus_ticket(video_id))


class SaveIntroVideoView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id is required."}, status=400)

        ep = _get_expert(request.user)
        ep.intro_video_bunny_id = video_id
        ep.intro_video_status = 1  # Uploaded
        ep.save(update_fields=["intro_video_bunny_id", "intro_video_status", "updated_at"])
        return Response(profile_ops.serialize_expert(ep))


class IntroVideoStatusView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def get(self, request):
        ep = _get_expert(request.user)

        if not ep.intro_video_bunny_id or ep.intro_video_status == 4:
            return Response(profile_ops.serialize_expert(ep))

        url = (
            f"https://video.bunnycdn.com/library/"
            f"{settings.BUNNY_LIBRARY_ID}/videos/{ep.intro_video_bunny_id}"
        )
        try:
            r = requests.get(url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=(5, 30))
            if r.status_code == 200:
                data = r.json()
                new_status = data.get("status", 0)
                ep.intro_video_status = new_status

                if new_status == 4 and not ep.intro_video_thumbnail_url:
                    thumb_file = data.get("thumbnailFileName", "")
                    cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
                    if thumb_file and cdn_host:
                        ep.intro_video_thumbnail_url = (
                            f"https://{cdn_host}/{ep.intro_video_bunny_id}/{thumb_file}"
                        )

                ep.save(update_fields=["intro_video_status", "intro_video_thumbnail_url", "updated_at"])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Bunny intro-video status check failed: %s", e)

        return Response(profile_ops.serialize_expert(ep))
