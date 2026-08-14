# Media security — what's done, what's deliberately deferred

**Status as of 2026-08-08.** Read `config/media_security.py`'s module
docstring first — this file is the "what's left" companion to it, for a
future session/chat to pick up without re-deriving everything from scratch.

## What shipped (commit `b211e3e` on `shiksha-backend`, `dev` branch)

nginx's `location /media/ { alias ...; }` served every file under
`MEDIA_ROOT` directly, with **zero authentication**, on the only live
`/media/` block that matters (`api.dev.shikshacom.com` — the other two
nginx files with a `/media/` block, `shiksha` and `shiksha-backend`, are
dead config on this box; `shikshacom.com`/`api.shikshacom.com` resolve to
the separate `shiksha-prod` host, `68.183.81.236`, confirmed via DNS
2026-08-08 — don't edit those two files thinking they're live, and don't
assume this fix reached prod. **This has NOT been deployed to
`shiksha-prod` yet.**).

Fixed by:
1. `config/secure_local_storage.py` — new `STORAGES["default"]` (when Bunny
   Storage creds are absent, which is the real case today) whose `.url()`
   routes private paths through `/api/media/secure/<path>` instead of the
   raw `/media/` path.
2. `config/media_views.py` — `secure_media_view`, the only door in. Checks
   real authorization, then hands bytes to nginx via `X-Accel-Redirect`
   (or streams them directly when `MEDIA_SERVED_BY_NGINX=False`, i.e. local
   dev/test with no nginx in front).
3. `config/media_security.py` — the authorization table itself. **Read
   this file's docstring before touching it** — it explains why PUBLIC and
   PRIVATE prefixes must be one length-ordered table, not two independent
   checks (a real bug caught before shipping: bare `"teachers/"` public
   and `"teachers/certificates/"` private share a parent, and two separate
   `startswith()` checks let the public one match first).
4. nginx (`dev.api` only) — mirrors the Python table's PUBLIC entries as
   explicit `alias` blocks; a blanket `location /media/ { return 403; }`
   catch-all replaces the old unauthenticated alias.

Gated content types: study materials, teacher application docs (KYC id
proofs, certificates, signed agreements, skill-application video/files),
learner profile photos, enrollment receipts, assignment files +
submissions, chat attachments (by conversation participancy), scholarship
guardian-verification documents, counseling session reports, skill
ad-subscription/payment receipts.

## Update 2026-08-13: `documents/` (Explore Library) gated

Found while investigating a live "download fails with 401" bug report on
an Explore Library document. Correcting a stale claim in the original
version of this doc: `documents.Document` has **no** `visibility` field —
that field exists on the unrelated `Collection` model. `Document` reads
are already `AllowAny` end to end (`DocumentDetailView`,
`RecordDownloadView`, etc. in `documents/views.py`), gated only by
`is_removed` (moderator soft-hide) — there's no per-object owner/visibility
restriction to reuse, it's simpler than that: every non-removed document is
public. Added `_check_explore_document` to `config/media_security.py`,
mirroring that exactly (`Document.objects.filter(file=name,
is_removed=False).exists()`), and wired `"explore/documents/"` into
`_RULES`. `exploreApi`'s `USE_MOCK` status should still be checked before
assuming this is the only explore-library gap — this fixes the file-serving
layer, not the API layer.

## Deliberately NOT gated yet — deny-by-default, not silently open

These fall through to `is_authorized`'s fallback (`staff-only`), so
nothing is exposed — but nothing works for a real non-staff user either
until someone adds a proper rule. Each needs its own investigation, not a
copy-paste of an existing rule:

- **`forum/` attachments** (`forum/models.py`'s `Attachment` /
  `_forum_attachment_path`). Needs `ForumCategory`-level visibility rules
  threaded through — some categories may be staff-only or role-gated,
  some may be open to every authenticated user. Check `forum/views.py`'s
  existing post-read permission logic and mirror it exactly; don't
  approximate.
- **`avatar/`, `profiles/photos/`** — 4 and 3 files respectively exist on
  the dev droplet's disk, but **no model in the current codebase
  references either `upload_to` string**. Likely orphaned from a renamed
  or removed field. Before writing a rule: figure out whether these are
  dead weight (safe to leave denied forever, or even delete) or whether
  some model's `upload_to` was refactored without a matching data
  migration for the old rows. Don't guess a rule for a model you can't find.

## Also confirmed but not yet acted on

- **`courses/thumbnails/`, `boards/logos/`** are in the PUBLIC table (both
  in Python and nginx) but **zero files exist in either directory on disk
  as of 2026-08-08** — likely because `Board.logo` uses an `imagekit`
  `ProcessedImageField`, which may resolve storage/caching differently
  from a plain `ImageField`. Not broken, just unverified with real data —
  re-check once someone actually uploads a board logo or course thumbnail.
- **Production (`shiksha-prod`, `68.183.81.236`) has not received any of
  this.** The Django-side fix (storage class swap) is safe to deploy
  there the same way — but the nginx change needs redoing against
  whichever of prod's nginx files is actually live for
  `api.shikshacom.com`/`shikshacom.com` (verify via DNS the same way this
  session did for dev — don't assume file names mean anything).
- **Bunny Storage.** If `BUNNY_STORAGE_ZONE`/`BUNNY_STORAGE_API_KEY` are
  ever set in this environment, `STORAGES["default"]` silently becomes
  `BunnyStorage` instead of `SecureLocalStorage` (see
  `config/settings_base.py`) — and `BunnyStorage.url()` returns a flat,
  **unauthenticated public CDN URL** for every field, including the
  private ones this whole effort just gated. If Bunny Storage is ever
  turned on, this entire fix needs re-doing against Bunny's own
  token-authentication feature (a paid/config feature on their end,
  unverified — see `BUNNY_KEY_EXPOSURE_TODO.md` for the parallel Bunny
  Stream key issue, same vendor, same "needs their current docs open"
  caveat).
