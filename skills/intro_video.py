"""skills/intro_video.py — one place for the Bunny intro-clip rules.

The expert-level clip (`ExpertProfile`) and the per-listing clip
(`SkillListing`) carry the same four fields and obey the same rules, so the
Bunny round trip lives here once instead of being copy-pasted across
`views_intro_video.py` and `listing_intro_video.py`.

Two things this module exists to get right, both of which were previously
wrong in ways that only showed up in production data:

1. **A saved clip is synced, not assumed.** The save endpoints used to write
   `intro_video_status = 1` ("Uploaded") and stop. Nothing else ever advanced
   it, because the only code that could — the status endpoint — had no caller
   on the expert path. Since `intro_video_embed_url()` returns None below
   status 4, every expert clip ever uploaded was invisible on every public
   surface while Bunny happily reported it finished. Saving now asks Bunny
   what the real status is.

2. **The duration limit is enforced on Bunny's own number.** The dashboard
   checks duration before uploading, but that is a courtesy to the person
   uploading, not a control — a caller that skips the browser still must not
   end up with a ten-minute clip on a public profile.

Bunny reports `length` as 0 until it has processed the file, so the limit is
applied on the first sync that returns a positive length. That is normally
the save call itself, and otherwise the next status poll.

The four fields this module reads and writes are duck-typed rather than
declared on a shared base class — `ExpertProfile` and `SkillListing` live in
different modules with different inheritance, and a mixin would be a bigger
change than the behaviour warrants.
"""
import logging

import requests
from django.conf import settings

log = logging.getLogger(__name__)

# Bunny's own status vocabulary, mirrored by
# ExpertProfile.INTRO_VIDEO_STATUS_CHOICES.
STATUS_CREATED = 0
STATUS_UPLOADED = 1
STATUS_FINISHED = 4
STATUS_ERROR = 5

# The advertised limit for an intro clip. One minute, matching the copy in
# the teacher dashboard — change both together.
MAX_INTRO_VIDEO_SECONDS = 60

TOO_LONG_MESSAGE = (
    "That clip is {seconds} seconds long. Intro videos must be "
    "{limit} seconds or shorter — please trim it and upload again."
)

_TIMEOUT = (5, 30)


def _api_root():
    return f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos"


def _headers(json=False):
    h = {"AccessKey": settings.BUNNY_API_KEY}
    if json:
        h["Content-Type"] = "application/json"
    return h


class BunnyUnavailable(Exception):
    """Bunny could not be reached, or refused the request.

    Raised only by `create_bunny_video`, where there is nothing useful to fall
    back to. Reads (`fetch_bunny_video`) return None instead, so a transient
    outage degrades to "status unchanged" rather than losing a real clip.
    """


def create_bunny_video(title):
    """Create an empty Bunny video and return its guid."""
    try:
        r = requests.post(
            _api_root(), json={"title": title}, headers=_headers(json=True), timeout=_TIMEOUT
        )
    except requests.RequestException as e:
        log.warning("Bunny intro-video create failed: %s", e)
        raise BunnyUnavailable("Could not reach the video service.") from e
    if r.status_code not in (200, 201):
        log.warning("Bunny intro-video create returned %s: %s", r.status_code, r.text[:300])
        raise BunnyUnavailable("The video service refused the request.")
    return r.json()["guid"]


def fetch_bunny_video(video_id):
    """Return Bunny's metadata dict for `video_id`, or None if unreachable.

    None means "we learned nothing" — never "the video is gone". Callers must
    leave existing state alone rather than downgrading it on a network blip.
    """
    if not video_id:
        return None
    try:
        r = requests.get(
            f"{_api_root()}/{video_id}", headers=_headers(), timeout=_TIMEOUT
        )
    except requests.RequestException as e:
        log.warning("Bunny intro-video status check failed: %s", e)
        return None
    if r.status_code != 200:
        log.warning("Bunny intro-video status check returned %s", r.status_code)
        return None
    return r.json()


_VIDEO_FIELDS = (
    "intro_video_bunny_id",
    "intro_video_status",
    "intro_video_thumbnail_url",
    "intro_video_duration",
)


def snapshot(obj):
    """Capture the current clip state so a failed replacement can undo itself."""
    return {f: getattr(obj, f) for f in _VIDEO_FIELDS}


def restore(obj, snap):
    """Put a snapshot back verbatim.

    Deliberately does NOT re-sync from Bunny: the previous clip's state was
    already correct, and asking again only adds a request that could mark a
    perfectly good clip as failed if Bunny happens to be having a bad minute.
    """
    for field, value in snap.items():
        setattr(obj, field, value)
    obj.save(update_fields=list(snap) + ["updated_at"])


def attach(obj, video_id):
    """Point `obj` at a new clip, clearing everything derived from the old one.

    A thumbnail or duration outliving the video it described is worse than
    having none — it makes a stale clip look current.
    """
    obj.intro_video_bunny_id = video_id
    obj.intro_video_status = None
    obj.intro_video_thumbnail_url = ""
    obj.intro_video_duration = None
    obj.save(update_fields=list(_VIDEO_FIELDS) + ["updated_at"])


def needs_sync(obj):
    """True when asking Bunny could still tell us something new.

    A finished clip whose duration and thumbnail are already recorded is
    terminal — polling it again just burns a request per page load.
    """
    if not obj.intro_video_bunny_id:
        return False
    if obj.intro_video_status != STATUS_FINISHED:
        return True
    return obj.intro_video_duration is None or not obj.intro_video_thumbnail_url


def sync_intro_video(obj, save=True):
    """Pull Bunny's current state onto `obj` and apply the duration limit.

    Returns `(changed, error)`. `error` is a human-readable string when the
    clip is rejected for length, in which case the status has been set to
    Error and the clip will not resolve to an embed URL. The caller decides
    whether that becomes a 400 (someone is waiting on the response) or just a
    stored state (a background poll).
    """
    data = fetch_bunny_video(obj.intro_video_bunny_id)
    if data is None:
        return False, None

    status = data.get("status", STATUS_CREATED)
    # Bunny reports 0 until the file has been processed; treat that as
    # "not known yet" rather than "a zero-second video".
    length = data.get("length") or None

    error = None
    if length is not None and length > MAX_INTRO_VIDEO_SECONDS:
        status = STATUS_ERROR
        error = TOO_LONG_MESSAGE.format(seconds=length, limit=MAX_INTRO_VIDEO_SECONDS)

    changed = []
    if obj.intro_video_status != status:
        obj.intro_video_status = status
        changed.append("intro_video_status")
    if length is not None and obj.intro_video_duration != length:
        obj.intro_video_duration = length
        changed.append("intro_video_duration")

    if status == STATUS_FINISHED and not obj.intro_video_thumbnail_url:
        thumb = data.get("thumbnailFileName", "")
        cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
        if thumb and cdn_host:
            obj.intro_video_thumbnail_url = (
                f"https://{cdn_host}/{obj.intro_video_bunny_id}/{thumb}"
            )
            changed.append("intro_video_thumbnail_url")

    if changed and save:
        obj.save(update_fields=changed + ["updated_at"])
    return bool(changed), error
