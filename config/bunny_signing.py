"""config/bunny_signing.py — Bunny Stream TUS upload signing.

Bunny's regular PUT-based video upload endpoint
(`/library/{id}/videos/{videoId}`) only accepts the master library
`AccessKey` header — there is no scoped variant of it, so handing that URL
+ key to a browser gives every teacher full read/write/delete over every
video in the library (see BUNNY_KEY_EXPOSURE_TODO.md).

Bunny's TUS resumable-upload endpoint accepts a per-video, time-limited
SHA256 signature instead: `sha256(library_id + api_key + expire + video_id)`.
The signature is computed here, server-side, with the real key never
leaving this process — the browser only ever sees the signature + expiry.
"""
import hashlib
import time

from django.conf import settings

DEFAULT_EXPIRY_SECONDS = 3600


def bunny_tus_ticket(video_id, expiry_seconds=DEFAULT_EXPIRY_SECONDS):
    library_id = settings.BUNNY_LIBRARY_ID
    api_key = settings.BUNNY_API_KEY
    expire = int(time.time()) + expiry_seconds
    signature = hashlib.sha256(
        f"{library_id}{api_key}{expire}{video_id}".encode()
    ).hexdigest()
    return {
        "video_id": video_id,
        "library_id": library_id,
        "expire": expire,
        "signature": signature,
    }
