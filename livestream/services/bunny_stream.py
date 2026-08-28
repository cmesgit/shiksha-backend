"""The Bunny Stream side of automatic class recording.

Phase 3. LiveKit egress can write to Bunny *Storage* but not Bunny *Stream*,
and Stream is the product the entire existing playback path speaks —
`SessionRecording.bunny_video_id`, the 0–5 status codes mirroring Bunny's own,
and `config/bunny_signing.py`'s token-authenticated embed. So the recording
hops once: egress → Storage → Stream's own fetch endpoint → everything
downstream unchanged.

THE UNCOMFORTABLE PART, stated plainly because it constrains the design:
`POST /videos/{guid}/fetch` takes a plain URL and cannot read a signed one.
The raw mp4 therefore has to be publicly readable on a pull zone for as long
as the fetch takes. Three things contain that, and all three matter:

  · the egress zone has its OWN pull zone, serving nothing else
    (config/settings_base.py refuses to share the CMS zone),
  · object keys carry a random segment, so the URL cannot be guessed from
    the session id alone (livestream/services/egress.py::storage_key_for),
  · phase 4 deletes the object as soon as Stream has ingested it.

Credentials note: this module uses BUNNY_API_KEY (the Stream library key),
NOT the BUNNY_EGRESS_* storage credentials. They are different products.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# (connect, read) — matches config/bunny_storage.py. A hung Bunny connection
# must not tie up a Celery worker indefinitely.
TIMEOUT = (5, 30)


def _library_base():
    base = (getattr(settings, "BUNNY_STREAM_URL", "") or
            "https://video.bunnycdn.com").rstrip("/")
    return f"{base}/library/{settings.BUNNY_LIBRARY_ID}"


def _headers(json=False):
    h = {"AccessKey": settings.BUNNY_API_KEY}
    if json:
        h["Content-Type"] = "application/json"
    return h


def public_url_for(storage_key):
    """The pull-zone URL Bunny Stream will fetch the raw mp4 from.

    Returns "" when no egress pull zone is configured, which is a
    misconfiguration rather than a transient failure — the caller records it
    instead of retrying forever.
    """
    host = (getattr(settings, "BUNNY_EGRESS_PULL_HOST", "") or "").strip("/")
    if not host:
        return ""
    return f"https://{host}/{storage_key.lstrip('/')}"


def create_video_slot(title):
    """Create an empty Bunny Stream video and return its guid.

    Same call CreateVideoSlotView makes for teacher uploads
    (courses/views_recordings.py), deliberately: an automatic recording must
    end up as the same kind of Stream video as a manual one, or the shared
    playback path stops being shared.
    """
    r = requests.post(
        f"{_library_base()}/videos",
        json={"title": title[:255]},
        headers=_headers(json=True),
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Bunny Stream slot creation failed: {r.status_code} {r.text[:300]}"
        )
    guid = (r.json() or {}).get("guid")
    if not guid:
        raise RuntimeError(f"Bunny Stream returned no guid: {r.text[:300]}")
    return guid


def fetch_into_video(guid, url):
    """Tell Bunny Stream to pull `url` into video `guid`.

    Returns None on success and raises on failure. The call is asynchronous on
    Bunny's side: a 200 means the fetch was accepted, not that the video is
    playable — that is what phase 4's status poll is for.
    """
    r = requests.post(
        f"{_library_base()}/videos/{guid}/fetch",
        json={"url": url},
        headers=_headers(json=True),
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Bunny Stream fetch failed: {r.status_code} {r.text[:300]}"
        )
    logger.info("Bunny Stream fetch accepted for %s", guid)
