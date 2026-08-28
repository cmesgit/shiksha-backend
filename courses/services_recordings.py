"""Shared Bunny Stream status sync for SessionRecording.

Extracted from `CheckVideoStatusView` (courses/views_recordings.py) so the
client-polled endpoint and the Celery sweep that phase 4 of automatic class
recording added cannot drift apart. Copying it instead would have duplicated
a real bug fix: the duration capture below is the ONLY place in the codebase
that reads Bunny's `length`, and the early-return condition is deliberately
"status 4 AND we have a duration" rather than status alone — on status alone,
any recording that reached READY before duration capture existed could never
acquire one, because nothing else fetches it. That in turn left
`VideoProgress.percent_complete` permanently null (a progress bar pinned at
0%) and made the auto-complete branch of SaveVideoProgressView unreachable,
since both are computed from duration.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = (5, 30)


def is_settled(recording):
    """True when there is nothing left to ask Bunny about this recording."""
    return recording.status == 4 and bool(recording.duration_seconds)


def refresh_from_bunny(recording):
    """Sync one recording's status, duration and thumbnail from Bunny Stream.

    Returns True if anything was written. Best-effort: a Bunny outage must
    neither raise into a client poll nor abort a sweep over other recordings.
    """
    if is_settled(recording):
        return False

    url = (
        f"https://video.bunnycdn.com/library/"
        f"{settings.BUNNY_LIBRARY_ID}/videos/{recording.bunny_video_id}"
    )

    try:
        r = requests.get(
            url, headers={"AccessKey": settings.BUNNY_API_KEY}, timeout=TIMEOUT)
    except Exception as e:
        logger.warning("Bunny status check failed for %s: %s", recording.pk, e)
        return False

    if r.status_code != 200:
        logger.warning(
            "Bunny status check for %s returned %s", recording.pk, r.status_code)
        return False

    # Parsing and writing are inside the guard too, not just the HTTP call.
    # The original inline version wrapped this whole block, so a malformed
    # Bunny body returned the recording unchanged rather than 500-ing a
    # client's status poll; narrowing the guard to the request alone would
    # have been a silent behaviour change in exactly the case the guard
    # exists for.
    try:
        data = r.json()
        recording.status = data.get("status", 0)

        length = data.get("length")
        try:
            length = int(length)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            recording.duration_seconds = length

        if recording.status == 4 and not recording.thumbnail_url:
            thumb_file = data.get("thumbnailFileName", "")
            cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
            if thumb_file and cdn_host:
                recording.thumbnail_url = (
                    f"https://{cdn_host}/{recording.bunny_video_id}/{thumb_file}"
                )

        recording.save(
            update_fields=["status", "thumbnail_url", "duration_seconds"])
    except Exception as e:
        logger.warning(
            "Bunny status parse/save failed for %s: %s", recording.pk, e)
        return False
    return True
