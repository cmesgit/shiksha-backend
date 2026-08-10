"""skills/listing_intro_video.py — per-LISTING Bunny intro clip.

The point of multi-skill is that a guitar clip does not advertise a welding
class, so the intro video moves from "one per expert" to "one per listing".

Mirrors views_intro_video.py's expert-level flow exactly — same three steps,
same Bunny calls, same status codes — but scoped to a listing the caller owns:

    POST /skill/teacher/listings/<id>/intro-video/            → {video_id, library_id, expire, signature}
    POST /skill/teacher/listings/<id>/intro-video/save/       ← {video_id}
    GET  /skill/teacher/listings/<id>/intro-video/status/     → {intro_video_status, ...}

The single POST returns the upload ticket in one round trip (the expert flow
splits create + sign across two calls; there is no reason for a second hop).
The browser then resumable-uploads the file straight to Bunny's TUS endpoint
using that per-video signature (never the master AccessKey) — see
config/bunny_signing.py and the frontend's useBunnyUpload hook.
"""
import logging

import requests
from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .listing_views import expert_or_403
from config.bunny_signing import bunny_tus_ticket

log = logging.getLogger(__name__)


def _listing_or_404(request, listing_id):
    from rest_framework.exceptions import NotFound
    expert = expert_or_403(request)
    listing = expert.listings.filter(id=listing_id).first()
    if not listing:
        raise NotFound("Skill not found.")
    return listing


class ListingIntroVideoView(APIView):
    """POST — create the Bunny video and hand back a direct-upload ticket."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        url = f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos"
        headers = {"AccessKey": settings.BUNNY_API_KEY, "Content-Type": "application/json"}
        title = request.data.get("title") or f"{listing.title} — intro"
        try:
            r = requests.post(url, json={"title": title}, headers=headers, timeout=20)
        except requests.RequestException as e:
            log.warning("Bunny listing intro-video create failed: %s", e)
            return Response({"error": "Could not reach the video service."}, status=502)
        if r.status_code not in (200, 201):
            return Response({"error": r.text}, status=502)

        video_id = r.json()["guid"]
        return Response(bunny_tus_ticket(video_id))


class ListingIntroVideoSaveView(APIView):
    """POST — record the uploaded video against the listing (status 1 = Uploaded)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id is required."}, status=400)
        listing.intro_video_bunny_id = video_id
        listing.intro_video_status = 1
        listing.intro_video_thumbnail_url = ""
        listing.save(update_fields=[
            "intro_video_bunny_id", "intro_video_status",
            "intro_video_thumbnail_url", "updated_at",
        ])
        return Response({
            "intro_video_status": listing.intro_video_status,
            "intro_video_thumbnail_url": listing.intro_video_thumbnail_url,
        })


class ListingIntroVideoStatusView(APIView):
    """GET — poll Bunny while it transcodes; 4 = Finished, 5 = Error."""
    permission_classes = [IsAuthenticated]

    def get(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        if not listing.intro_video_bunny_id or listing.intro_video_status == 4:
            return Response({
                "intro_video_status": listing.intro_video_status,
                "intro_video_thumbnail_url": listing.intro_video_thumbnail_url,
                "intro_video_embed_url": listing.intro_video_embed_url(),
            })

        url = (
            f"https://video.bunnycdn.com/library/"
            f"{settings.BUNNY_LIBRARY_ID}/videos/{listing.intro_video_bunny_id}"
        )
        try:
            r = requests.get(url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=20)
            if r.status_code == 200:
                data = r.json()
                listing.intro_video_status = data.get("status", 0)
                if listing.intro_video_status == 4 and not listing.intro_video_thumbnail_url:
                    thumb = data.get("thumbnailFileName", "")
                    cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
                    if thumb and cdn_host:
                        listing.intro_video_thumbnail_url = (
                            f"https://{cdn_host}/{listing.intro_video_bunny_id}/{thumb}"
                        )
                listing.save(update_fields=[
                    "intro_video_status", "intro_video_thumbnail_url", "updated_at",
                ])
        except requests.RequestException as e:
            log.warning("Bunny listing intro-video status check failed: %s", e)

        return Response({
            "intro_video_status": listing.intro_video_status,
            "intro_video_thumbnail_url": listing.intro_video_thumbnail_url,
            "intro_video_embed_url": listing.intro_video_embed_url(),
        })
