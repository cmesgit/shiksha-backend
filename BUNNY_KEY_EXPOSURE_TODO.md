# Bunny Stream master API key exposed to teacher browsers — needs a decision, not yet fixed

**Status as of 2026-08-08: confirmed live, NOT fixed.** Written so another
chat/session can pick this up with full context, without re-deriving it.

## The bug

`courses/views_recordings.py`'s `SignedUploadUrlView` (around line 108-122)
returns `settings.BUNNY_API_KEY` — the real, full-access Bunny Stream
library API key — directly in a JSON response to any authenticated teacher:

```python
class SignedUploadUrlView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request):
        ...
        return Response({
            "upload_url": f"https://video.bunnycdn.com/library/{settings.BUNNY_LIBRARY_ID}/videos/{video_id}",
            "access_key": settings.BUNNY_API_KEY,
        })
```

The frontend (`shiksha-teacher-dashboard/src/pages/UploadRecording.jsx`,
around line 98-116) puts that key straight into an `AccessKey` request
header and does a direct browser→Bunny `PUT`. Any teacher opening devtools
can read the key from the response and then use it directly against
Bunny's API — not just to upload their own recordings, but to **read,
overwrite, or delete every video in the entire library**, including other
teachers' recordings and Skill Dev expert intro videos
(`skills/views_intro_video.py` uses the same key).

**Severity context**: this requires an already-authenticated teacher
account — not open to students or the public. That's why it wasn't fixed
in the same session as the `/media/` auth gap (a much larger, more
severe, more urgent exposure that WAS fixed — see
`MEDIA_SECURITY_TODO.md`).

## Why this wasn't just fixed blind

Two real options exist, and picking the wrong one either breaks a working
feature or ships something that looks fixed but isn't:

### Option A — Bunny's signature-based (TUS) upload

Bunny Stream supports an `AuthorizationSignature`/`AuthorizationExpire`
scheme for resumable (TUS-protocol) uploads, computed as a hash of
`library_id + api_key + expiration + video_id` — the browser never sees
the raw key, only a time-limited, video-scoped signature computed
**server-side**.

**Why this wasn't implemented tonight**: I could not verify Bunny's
*current* exact TUS signature format/headers without live API docs access
in this session. Their classic direct-`PUT` upload (what's used today)
and their TUS resumable-upload signature scheme are genuinely different
APIs — guessing at the hash construction risks either (a) a broken upload
that looks like it works until a real file is tried, or (b) silently
weakening security in a way that's hard to notice (e.g. an unbounded
expiration, or a signature that doesn't actually scope to one video).
**Before implementing: open Bunny's current Stream API docs and confirm
the exact TUS signature construction and headers required.** This also
likely requires switching the frontend from a plain `XMLHttpRequest` PUT
to a TUS client library (e.g. `tus-js-client`) — not a small frontend
change, since TUS is a chunked/resumable protocol, not a single PUT.

### Option B — Proxy uploads through the Django backend

Browser uploads to a new Django endpoint (multipart), Django forwards the
bytes to Bunny server-side using the real key. No external API research
needed — this is a straightforward Django view using the exact same
`requests.put(...)` pattern already used elsewhere in
`courses/views_recordings.py`.

**Trade-off**: recordings are validated up to 4GB
(`UploadRecording.jsx`'s own `ALLOWED_TYPES`/size check). Proxying that
through the single Django app server (per project memory: a single
2vCPU/4GB box) ties up a worker/connection for the full upload duration
instead of a direct browser→Bunny transfer. At low upload volume this is
probably fine; at any real scale it's a real bottleneck. Whoever picks
this option should also add a request size/duration limit and think about
async/streaming (Django's request body streaming + `requests`' streaming
upload support can avoid buffering the whole file in memory, but this
needs testing under this project's actual ASGI/uvicorn setup — verify,
don't assume).

## Also worth fixing while touching this file

`DeleteRecordingView`'s Bunny-asset-leak bug was already fixed this
session (`courses/views_recordings.py`) — deleting a `SessionRecording`
row now also calls Bunny's delete API, best-effort. Not related to this
key-exposure issue, just noted so whoever picks this up doesn't
re-discover it.

## Recommendation for whoever picks this up

Start with Bunny's current docs open side-by-side, confirm which option
their TUS signature scheme actually supports today, and only then choose
A vs B — don't implement either from memory of "how Bunny probably works."
