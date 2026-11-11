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
from base64 import b64encode

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


# ---------------------------------------------------------------------------
# 3. STORAGE / CDN FILE DELIVERY  (`bunny_storage_url`)
#
# A THIRD mechanism with a THIRD key, unrelated to the two above. Bunny Stream
# (video) and Bunny Edge Storage (files) are separate products with separate
# pull zones and separate token-authentication keys.
#
# Why this exists: `config/bunny_storage.py`'s `url()` returns a bare public
# CDN URL for EVERY FileField, so `config/media_security.py`'s deny-by-default
# rules — written for teacher ID scans, learner photos, guardian KYC documents
# and payment receipts — never run in production. They only guard the local
# SecureLocalStorage path, which prod does not use.
#
# This helper does NOT close that hole on its own. Signing a URL only matters
# once Bunny is configured to REQUIRE a token, and that is a dashboard change
# plus a decision recorded in the rollout notes — a pull zone serves its whole
# storage zone, so token auth cannot be scoped to `teachers/` while leaving
# `content/` public on the same zone. Until that is resolved, this returns
# unsigned URLs and says so, exactly like bunny_embed_url does.
# ---------------------------------------------------------------------------

STORAGE_EXPIRY_SECONDS = 15 * 60


def storage_token_configured():
    """True when this deployment can actually sign storage URLs."""
    return bool(getattr(settings, "BUNNY_STORAGE_TOKEN_KEY", ""))


def is_private_storage_path(name):
    """True when `name` belongs to a prefix that must never be public.

    Driven by settings so the list is auditable in one place rather than
    re-derived at each call site. A path not on the list is treated as public,
    which is the safe default ONLY because the consequence of a wrong answer
    here is a broken image, not a leak — the leak is prevented by Bunny
    requiring a token, not by this function.
    """
    prefixes = getattr(settings, "BUNNY_PRIVATE_PREFIXES", ())
    key = str(name).lstrip("/")
    return any(key.startswith(p) for p in prefixes)


def bunny_storage_token(path, expires):
    """Bunny CDN token-authentication signature for a storage path.

    Bunny's documented scheme is
    ``Base64(SHA256_RAW(token_key + path + expires))`` with the base64 made
    URL-safe: ``+`` → ``-``, ``/`` → ``_``, ``=`` stripped.

    `path` must be the absolute path Bunny sees, with a leading slash and no
    query string, and `expires` a UNIX timestamp in SECONDS.

    NOT verified against a live Bunny zone — no zone has token authentication
    enabled yet. A wrong byte here fails CLOSED (Bunny rejects the token and
    returns 403), so the failure mode is a broken download rather than an
    ungated one, but do not treat this as proven until a staging zone has
    served a signed request.
    """
    key = settings.BUNNY_STORAGE_TOKEN_KEY
    digest = hashlib.sha256(f"{key}{path}{expires}".encode()).digest()
    return (
        b64encode(digest)
        .decode()
        .replace("\n", "")
        .replace("+", "-")
        .replace("/", "_")
        .replace("=", "")
    )


def bunny_storage_url(name, *, host, expiry_seconds=STORAGE_EXPIRY_SECONDS):
    """Return (url, expires_or_None, signed_bool) for a stored file.

    Unsigned — byte-for-byte the previous behaviour — when the path is public,
    when no token key is configured, or both. The caller is told which, so a
    serializer can report `token_auth: false` instead of implying a private
    file is gated when it is not.
    """
    path = "/" + str(name).lstrip("/")
    base = f"https://{host.rstrip('/')}{path}"

    if not is_private_storage_path(name) or not storage_token_configured():
        return base, None, False

    expires = int(time.time()) + expiry_seconds
    token = bunny_storage_token(path, expires)
    return f"{base}?token={token}&expires={expires}", expires, True
