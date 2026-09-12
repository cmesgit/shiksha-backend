"""content/demo_video_bunny.py — the Bunny round trip for landing demo clips.

Two callers share this: ``manage.py sync_demo_videos`` and the upload views on
``DemoVideoAdmin``. Before the upload flow existed the sync rules lived inside
the command, which was fine while the command was the only way a clip's state
ever changed. It no longer is — the admin attaches a guid and immediately wants
to know whether Bunny finished — and two copies of "what does Bunny say" is how
the site ends up disagreeing with itself about whether a clip is playable.

The HTTP layer is deliberately NOT re-implemented here. ``skills/intro_video.py``
already wraps Bunny's video API with the right timeouts and the right failure
semantics, and the sync command has imported ``fetch_bunny_video`` from it since
the day it was written. A third copy of ``requests.post(...)`` would be a third
place to get the AccessKey header wrong.

## Uploads go browser → Bunny, never through Django

This is the whole reason the upload path can exist at all. Server-initiated
uploads to Bunny Stream do not work from our hosts: the documented simple PUT
answers ``200 {"success":true}`` and stores zero bytes, and walking the TUS flow
by hand from the server fares no better — ``201`` on create, ``204`` with a
full ``Upload-Offset`` on PATCH, and the video still sits at ``status 2`` with
``storageSize 0`` a quarter of an hour later. Both were measured on 2026-09-11,
on two files, across several attempts. A dashboard upload to the same library
the same day encoded fine, so the account is healthy and the fault is specific
to uploads we start.

So Django's entire job here is to mint an empty video and sign a short-lived
ticket. The bytes go from the editor's browser straight to Bunny, which is the
path ``skills/views_intro_video.py`` and ``courses/views_recordings.py`` have
used in production all along.

One consequence worth keeping in mind: a slot whose upload fails cannot be
retried. A second PUT to the same guid is refused with "The video has already
been uploaded." even when it holds nothing, so recovery means a fresh slot —
which is what ``create_upload_slot`` gives every attempt.
"""
import logging

from django.conf import settings

from config.bunny_signing import bunny_tus_ticket, upload_expiry_for_size
from skills.intro_video import (  # noqa: F401  (BunnyUnavailable is re-exported)
    BunnyUnavailable,
    create_bunny_video,
    fetch_bunny_video,
)

log = logging.getLogger(__name__)

# What the video is called in the Bunny dashboard when the browser sends no
# filename. Not user-visible on the site — the row's own `title` is.
SLOT_TITLE_FALLBACK = "landing-demo-video"

# Bunny's own encoding vocabulary, in words. `DemoVideo.bunny_status` stores
# the raw number because that is what the API returns and what the public list
# endpoint compares against; an editor should never have to look up what 3
# means.
STATUS_LABELS = {
    0: "Created",
    1: "Uploaded",
    2: "Processing",
    3: "Transcoding",
    4: "Finished",
    5: "Error",
    6: "Upload failed",
}


def status_label(status):
    """Bunny's status as a word, falling back to the raw number."""
    if status is None:
        return "Not checked yet"
    return STATUS_LABELS.get(status, f"Unknown ({status})")


def create_upload_slot(title="", size_bytes=None):
    """Create an empty Bunny video and return a TUS ticket for it.

    Creating and signing in one call is not a shortcut, it is the access
    control. ``skills`` splits these into two endpoints and therefore needs
    ``PendingIntroVideoUpload`` to prove the caller owns the guid it asks to
    have signed; here the only guid that can ever be signed is one this call
    just minted, so there is nothing to own and nothing to check.

    Raises ``BunnyUnavailable`` if Bunny refuses or cannot be reached. There is
    no useful fallback — without a slot there is nowhere to put the bytes.
    """
    video_id = create_bunny_video(title or SLOT_TITLE_FALLBACK)
    return bunny_tus_ticket(video_id, upload_expiry_for_size(size_bytes))


def attach(video, video_id):
    """Point `video` at a new clip, clearing everything derived from the old one.

    A duration or thumbnail outliving the clip it described is worse than
    having none: the menu keeps advertising "0:53" for a video that is now
    something else, and nothing ever catches it. Blanking them also drops the
    row off the site until the next sync confirms the new clip finished, which
    is the correct reading of "we do not know yet".
    """
    video.bunny_video_id = video_id
    video.bunny_status = None
    video.duration_seconds = None
    video.thumbnail_url = ""
    video.save(update_fields=[
        "bunny_video_id", "bunny_status", "duration_seconds", "thumbnail_url",
        "updated_at",
    ])


def sync_demo_video(video, *, save=True):
    """Pull Bunny's current state onto `video`.

    Returns ``(changed, data)``. ``data`` is Bunny's raw dict, or None when
    Bunny could not be reached — which means "we learned nothing", never "the
    video is gone". Callers must leave the stored values alone in that case
    rather than blanking a good duration over a network blip.

    ``changed`` lists the field names that were modified. With ``save=False``
    the fields are set on the in-memory object but not written, which is what
    the command's ``--dry-run`` reports on.
    """
    data = fetch_bunny_video(video.bunny_video_id)
    if data is None:
        return [], None

    status = data.get("status")
    # Bunny reports `length: 0` until it has processed the file. That is "not
    # known yet", not "a zero-second video" — publishing it as 0 would print
    # "0:00" under the title.
    length = data.get("length") or None

    changed = []

    # Recorded even when it is not 4 (Finished), because the public list
    # endpoint gates on it: a clip that regresses, or never finishes, must go
    # back to hidden rather than keep serving on a stale value.
    if video.bunny_status != status:
        video.bunny_status = status
        changed.append("bunny_status")

    if length is not None and video.duration_seconds != length:
        video.duration_seconds = length
        changed.append("duration_seconds")

    thumb = data.get("thumbnailFileName", "")
    cdn_host = getattr(settings, "BUNNY_CDN_HOST", "")
    if thumb and cdn_host:
        url = f"https://{cdn_host}/{video.bunny_video_id}/{thumb}"
        if video.thumbnail_url != url:
            video.thumbnail_url = url
            changed.append("thumbnail_url")

    if changed and save:
        video.save(update_fields=changed + ["updated_at"])
    return changed, data
