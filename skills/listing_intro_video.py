"""skills/listing_intro_video.py — per-LISTING Bunny intro clip.

The point of multi-skill is that a guitar clip does not advertise a welding
class, so the intro video moves from "one per expert" to "one per listing".

Mirrors views_intro_video.py's expert-level flow — same steps, same status
codes — but scoped to a listing the caller owns:

    POST /skill/teacher/listings/<id>/intro-video/            → {video_id, library_id, expire, signature}
    POST /skill/teacher/listings/<id>/intro-video/save/       ← {video_id}
    GET  /skill/teacher/listings/<id>/intro-video/status/     → {intro_video_status, ...}

The single POST returns the upload ticket in one round trip (the expert flow
splits create + sign across two calls; there is no reason for a second hop).
The browser then resumable-uploads the file straight to Bunny's TUS endpoint
using that per-video signature (never the master AccessKey) — see
config/bunny_signing.py and the frontend's useBunnyUpload hook.

The Bunny calls and the duration limit live in skills/intro_video.py.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .listing_views import expert_or_403
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


def _listing_or_404(request, listing_id):
    from rest_framework.exceptions import NotFound
    expert = expert_or_403(request)
    listing = expert.listings.filter(id=listing_id).first()
    if not listing:
        raise NotFound("Skill not found.")
    return listing


def _video_state(listing):
    return {
        "intro_video_status": listing.intro_video_status,
        "intro_video_thumbnail_url": listing.intro_video_thumbnail_url,
        "intro_video_duration": listing.intro_video_duration,
        "intro_video_embed_url": listing.intro_video_embed_url(),
    }


class ListingIntroVideoView(APIView):
    """POST — create the Bunny video and hand back a direct-upload ticket."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        title = request.data.get("title") or f"{listing.title} — intro"
        try:
            video_id = create_bunny_video(title)
        except BunnyUnavailable as e:
            return Response({"error": str(e)}, status=502)
        return Response(bunny_tus_ticket(video_id))


class ListingIntroVideoSaveView(APIView):
    """POST — record the uploaded video against the listing.

    Asks Bunny for the real status instead of assuming "Uploaded", and
    refuses a clip longer than the limit.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"error": "video_id is required."}, status=400)

        previous = snapshot(listing)
        attach(listing, video_id)

        _, error = sync_intro_video(listing)
        if error:
            # A rejected replacement is a no-op — put the previous clip back.
            restore(listing, previous)
            return Response({"error": error}, status=400)

        if listing.intro_video_status is None:
            listing.intro_video_status = 1  # Uploaded; still transcoding.
            listing.save(update_fields=["intro_video_status", "updated_at"])

        return Response(_video_state(listing))


class ListingIntroVideoStatusView(APIView):
    """GET — poll Bunny while it transcodes; 4 = Finished, 5 = Error."""
    permission_classes = [IsAuthenticated]

    def get(self, request, listing_id):
        listing = _listing_or_404(request, listing_id)
        if needs_sync(listing):
            sync_intro_video(listing)
        return Response(_video_state(listing))
