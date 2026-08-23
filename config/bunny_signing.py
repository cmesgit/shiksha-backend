"""config/bunny_signing.py — Bunny Stream signing: TUS uploads AND playback.

Two separate Bunny mechanisms live here. They use DIFFERENT keys and
DIFFERENT hash inputs; do not merge them.

1. UPLOAD (`bunny_tus_ticket`)
   Bunny's regular PUT-based video upload endpoint
   (`/library/{id}/videos/{videoId}`) only accepts the master library
   `AccessKey` header — there is no scoped variant of it, so handing that
   URL + key to a browser gives every teacher full read/write/delete over
   every video in the library.

   Bunny's TUS resumable-upload endpoint accepts a per-video, time-limited
   SHA256 signature instead:
   `sha256(library_id + api_key + expire + video_id)`. The signature is
   computed here, server-side, with the real key never leaving this
   process — the browser only ever sees the signature + expiry.

2. PLAYBACK (`bunny_embed_url`)
   The iframe embed URL was, until now, built CLIENT-SIDE from a library id
   shipped in the bundle plus the `bunny_video_id` the API serializes to
   every viewer:

       https://iframe.mediadelivery.net/embed/{LIBRARY_ID}/{videoId}

   That URL is unauthenticated and permanent. A student could copy it out of
   devtools, cancel their subscription, and keep streaming forever — or post
   it publicly. Every check in `_require_recording_viewer` was bypassed by a
   single copied string.

   Bunny's answer is "Embed View Token Authentication": the embed URL carries
   `?token=<hex>&expires=<unix>` where

       token = SHA256_HEX(token_authentication_key + video_id + expires)

   and Bunny rejects the iframe with 403 once `expires` has passed. The
   signing key is a THIRD Bunny secret (library → Security → "Embed View
   Token Authentication"), distinct from BUNNY_API_KEY, and it must never
   reach the client. Callers therefore ask the server for a short-lived
   signed URL per playback instead of building one.

   IMPORTANT OPERATIONAL NOTE: signing is only *enforced* once the Bunny
   library itself has "Embed View Token Authentication" switched ON. With it
   off, Bunny happily serves both signed and unsigned URLs, so this module
   raises the ceiling but the ops toggle is what closes the door. See
   `BUNNY_STREAM_TOKEN_KEY` in settings_base.py.
"""
import hashlib
import time

from django.conf import settings

DEFAULT_EXPIRY_SECONDS = 3600

# A single TUS ticket has to survive the WHOLE transfer — Bunny rejects every
# chunk once `expire` passes and the client has no resume path, so a 4 GB
# lecture (the cap UploadRecording.jsx enforces) on a slow uplink used to lose
# the entire upload at the 60-minute mark. Grant an expiry derived from the
# declared file size instead, assuming a pessimistic ~1.2 Mbit/s uplink, and
# still cap it: the ticket is scoped to one video_id the caller already owns,
# so a longer life is cheap, but not unbounded.
MAX_UPLOAD_EXPIRY_SECONDS = 12 * 3600
ASSUMED_UPLOAD_BYTES_PER_SEC = 150 * 1024

# Playback tickets are the opposite trade-off: short enough that a copied URL
# is worthless within the hour, long enough to watch a full class without the
# player 403-ing mid-lecture. Bunny validates `expires` at REQUEST time (the
# iframe document load), not continuously, so this bounds how long a leaked
# link works, not how long a running playback lasts.
EMBED_EXPIRY_SECONDS = 4 * 3600

BUNNY_EMBED_BASE = "https://iframe.mediadelivery.net/embed"


def upload_expiry_for_size(size_bytes, default=DEFAULT_EXPIRY_SECONDS):
    """Seconds a TUS ticket should live to cover `size_bytes` of transfer."""
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        return default
    if size_bytes <= 0:
        return default
    needed = int(size_bytes / ASSUMED_UPLOAD_BYTES_PER_SEC)
    return max(default, min(MAX_UPLOAD_EXPIRY_SECONDS, needed))


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


def embed_token_configured():
    """True when this deployment can actually sign embed URLs.

    Kept separate from bunny_embed_url() so views/serializers can report the
    real state to ops (and to the client, as `token_auth: false`) instead of
    silently degrading to the old permanent URL and looking secure.
    """
    return bool(getattr(settings, "BUNNY_STREAM_TOKEN_KEY", ""))


def bunny_embed_token(video_id, expires):
    """SHA256_HEX(token_authentication_key + video_id + expires).

    Bunny's documented Embed View Token scheme. `expires` is a UNIX timestamp
    in SECONDS — Bunny explicitly rejects milliseconds.
    """
    key = settings.BUNNY_STREAM_TOKEN_KEY
    return hashlib.sha256(f"{key}{video_id}{expires}".encode()).hexdigest()


def bunny_embed_url(video_id, *, expiry_seconds=EMBED_EXPIRY_SECONDS, params=None):
    """A ready-to-embed iframe URL for `video_id`, signed when possible.

    Returns (url, expires_at_unix_or_None, signed_bool). `params` are extra
    player query args (start=, autoplay=…) — they ride alongside token/expires
    and are NOT part of the hash input, which covers the video id and expiry
    only.

    When BUNNY_STREAM_TOKEN_KEY is unset the URL is returned unsigned, exactly
    as the clients used to build it themselves. That is not a silent fallback:
    the caller is told (`signed=False`) and surfaces it, so a misconfigured
    environment still plays video rather than bricking every recording, while
    nobody is misled into thinking playback is gated.
    """
    library_id = settings.BUNNY_LIBRARY_ID
    if not library_id or not video_id:
        return None, None, False

    query = dict(params or {})
    expires = None
    signed = False
    if embed_token_configured():
        expires = int(time.time()) + expiry_seconds
        query["token"] = bunny_embed_token(video_id, expires)
        query["expires"] = expires
        signed = True

    from urllib.parse import urlencode

    url = f"{BUNNY_EMBED_BASE}/{library_id}/{video_id}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url, expires, signed
