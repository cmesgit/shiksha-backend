# Build guide — Explore community verification

Read `docs/EXPLORE_VERIFICATION.md` first; it is the spec. This file is the **order of work**.

Repo rules that apply here (from the workspace `CLAUDE.md`):

- **Backend first in every phase.** Never leave a frontend calling an endpoint
  that doesn't exist on the branch.
- **Stop at phase boundaries.** Each phase is run and reviewed before the next
  starts. A phase whose diff passes ~800 lines gets split.
- Every phase leaves existing tests passing and adds tests for its own
  behaviour: `python manage.py test` backend, `npm run build` for any frontend
  touched.
- The public site has its own tokens. **No literal hexes** from the design
  palette in components.
- `Admin-dashboard` uses `--admin-*` + `lucide-react`, has **no toast library**
  (local state on a 2.5s timer through `components/Toast.jsx`), and its
  `npm run build` fails locally by design — use `npx vite build`.

---

## ⚠ Before touching anything

All three repos are on branch **`feat/career-v3`** with **dirty working trees**
(the uncommitted session join/accept/cancel hardening work — 641 tests, see the
`session-join-accept-cancel-hardening-2026-11-12` memory). That work is
unrelated and must be preserved.

**No branch was created** and nothing has been committed. Switching branches
would have moved HEAD under someone else's uncommitted work, and this feature
is almost entirely *new files*, so it separates cleanly by path instead.

Do not stage, reset or revert anything you did not write. When this is
committed, name the paths — never `git add -A`, never `commit -a`.

### Files owned by this work

**`shiksha-backend`**
```
NEW   documents/fields.py                     field › subject › branch map
NEW   documents/set_signals.py                keeps set children populated
NEW   documents/verification_models.py        the five verification models
NEW   documents/verification.py               ALL the rules — nothing re-derives them
NEW   documents/verification_serializers.py
NEW   documents/verification_views.py
NEW   documents/tests_sets.py                 45 tests
NEW   documents/tests_verification.py         57 tests
NEW   documents/tests_verification_api.py     63 tests
NEW   documents/migrations/0010_documentfile_documenttopic.py
NEW   documents/migrations/0011_backfill_set_files_and_topics.py
NEW   documents/migrations/0012_verificationsettings_document_verification_status_and_more.py
NEW   global_settings/migrations/0018_globalsettings_explore_verification_enabled.py
EDIT  documents/models.py        children + 3 Document fields + re-exports
EDIT  documents/serializers.py   verification / topics / files on the card; detail panels
EDIT  documents/views.py         visibility gate, status filter, set children on upload
EDIT  documents/urls.py          the verification/ block
EDIT  documents/apps.py          one import
EDIT  config/upload_validation.py  LIBRARY_EXTS + LIBRARY_MAX_MB + offset signatures
EDIT  notifications/policy.py    three verbs, SMS OFF
EDIT  global_settings/{models,serializers,views,tests}.py
EDIT  accounts/auth_flow.py      one feature_flags key
```

**`shiksha-frontend`**
```
NEW   src/explore/verificationApi.js
NEW   src/explore/VerifyPage.jsx          /explore/verify, five tabs
NEW   src/explore/Verification.css
NEW   src/explore/MyLibraryPage.jsx       replaces the six-tab dashboard
EDIT  src/explore/components/ui.jsx       VerificationBadge + ReviewPrompt
EDIT  src/explore/DocumentPage.jsx        verification + history panels
EDIT  src/explore/ExploreBrowse.jsx       status filter row
EDIT  src/explore/ExploreToolbar.jsx      the Verify entry
EDIT  src/explore/UploadPage.jsx          multi-file, wider types, 50 MB
EDIT  src/explore/exploreApi.js           arrays → repeated multipart parts
EDIT  src/api/publicConfig.js             the flag's safe default
EDIT  src/components/App.jsx              two routes
EDIT  src/explore/Explore.css             upload file list + inline link
```

**`Admin-dashboard`**
```
NEW   src/api/admin_verification.js
NEW   src/pages/VerificationAdmin.jsx     /verification, three tabs
NEW   src/css/Verification.css
EDIT  src/App.jsx, src/components/AdminLayout.jsx
```

⚠ **`contexts/AuthContext.jsx` was deliberately NOT touched.** It is generated
from `shared/src/**`, and `shared/` currently has unrelated uncommitted work on
branch `feat/account-model-simplification` — propagating through it would mix
that work into this diff. The flag is read from `publicConfig.js` instead,
which is the right source anyway for a page guests can see.

`documents/research_workflow.py` and `documents/tests_research.py` also show as
modified — **those are not this work**, they belong to the pre-existing
uncommitted changes. Leave them alone.

---

## Phase 0 — groundwork (no user-visible change)

1. `GlobalSettings.explore_verification_enabled`, **default `False`**, plus
   migration. This is a **real gate**, like `content_studio_enabled` and unlike
   `quiz_v2_enabled`.
2. Expose it **both** ways:
   - read-only in `feature_flags` on `GET /api/accounts/me/`
     (`accounts/auth_flow.py:939`), and
   - in `PublicConfigView.PUBLIC_FLAGS` (`global_settings/views.py:48`).

   🔴 **Both are required.** Explore is browsable by anonymous visitors, so a
   flag delivered only on `/accounts/me/` never reaches the people who see the
   status badges. This is the same reasoning already recorded for
   `public_quiz_hub_enabled`, `live_ticker_enabled` and
   `admission_abroad_enabled`.
3. Admin-readable/writable via `PATCH /api/admin/settings/`.
4. **Record the baseline** in this file before writing feature code:
   `python manage.py test documents notifications global_settings accounts`,
   `npx vite build` in each frontend. Note what already fails so the count
   never silently changes.

> Remember: flipping a flag needs a **re-login** to take effect in
> `Admin-dashboard` — its `AuthContext.bootstrap()` restores `user` from
> `localStorage` and never refetches `/accounts/me/`.

**Baseline, recorded 2026-11-12:**

```
documents notifications global_settings accounts = 426 tests, OK (0 failures)
  (DJANGO_SETTINGS_MODULE=config.settings_test, .venv)
shiksha-frontend  npx vite build = (record before Phase 4)
Admin-dashboard   npx vite build = (record before Phase 6)
```

Nothing pre-existing fails in these apps. If the count moves, it was this work.

---

## Phase 1 — sets, files and tags

The design's unit is a **set of files with several tag paths**. The owner chose
to build this for real (2026-11-12), so it lands before verification, because
verification attaches to the set.

### 🔴 `Document` becomes the set. Do not add a wrapper model.

The tempting shape — a new `DocumentSet` that groups `Document` rows — is
wrong. **Nine models FK to `Document`**: `DocumentLike`, `SavedDocument`,
`Collection` (M2M), `Follow`, `Report`, `ModerationAction`, `DuplicateFlag`,
plus the research provenance link. A wrapper orphans every one of them and
breaks `/explore/doc/:id`, every saved document and every open report.

Instead:

- **`Document` keeps its identity and becomes the set.** Title, description,
  category, institution, language, owner, counts, moderation flags all stay
  exactly where they are. Every FK, URL and permission check keeps working.
- **New `DocumentFile`** — `document`, `file`, `filetype`, `original_name`,
  `size`, `pages`, `file_hash`, `order`. One row per file.
- **New `DocumentTopic`** — `document`, `field`, `subject`, `branch`, `order`.
  Several rows per set; this is the design's tag.

### Migration, in this order

1. Create `DocumentFile` and `DocumentTopic`.
2. **Backfill** one `DocumentFile` per existing `Document` from its `file` /
   `filetype` / `pages` / `file_hash`, and one `DocumentTopic` from its
   `subject` / `topic` with `field` derived from the subject→field map.
3. Leave `Document.file`, `filetype`, `pages`, `file_hash`, `subject` and
   `topic` **in place and in sync** for this release. Drop them only after a
   grep across **every app**, not just `documents/` — the last `is_active` read
   site turned out to be in `courses/views.py`.

> `Document` has `UniqueConstraint(owner, file_hash)` where not removed
> (`models.py:143`), which is the "you already uploaded this file" check. With
> several files per set that constraint moves to `DocumentFile` and keys on
> `(owner, file_hash)` through the parent. Port it deliberately — dropping it
> silently re-enables duplicate uploads.

### Taxonomy

`field` is derived from a static subject→field map unioned with what is
actually on visible documents, exactly how `taxonomy.py:subject_groups()`
already derives the two-level tree. Members can still invent a branch with no
approval. Fields: Engineering, Social Sciences, Sciences, Humanities, Commerce,
Exam Prep.

### 🔴 The backfill is only half the job

0011 converts the documents that exist **when it runs**. Anything created
afterwards is born with no children and renders as an empty set that no browse
facet can see — silently. There are three creation paths, not one:
`documents/views.py:341` (upload), `documents/research_views.py:140` (Research
Hub publishing a paper) and the Django admin.

`documents/set_signals.py` covers all three with a `post_save` receiver. It is
**backfill-only, never a rewrite** — from Phase 8 the parent columns hold only
the *first* file and the *first* tag, so a receiver that kept them "in step"
would delete the second file of every multi-file upload.

The same receiver also **propagates `is_removed` onto the files**. That is not
bookkeeping: `DocumentFile.is_removed` is half of the duplicate-upload
constraint, and every removal path is a targeted
`save(update_fields=[..., "is_removed", ...])` — the exact shape the backfill
half skips. Getting this wrong makes "delete it and upload the corrected
version" fail forever, and the refusal names a document the owner can no longer
see. It was caught by `documents.tests.OwnerDeleteTests`, which already existed.

### Status — DONE 2026-11-12

`documents notifications global_settings accounts` = **469 tests OK**
(baseline 426, +43 new, zero regressions). Nothing committed.

Not yet done in this phase, deliberately — they belong with the screens that
need them:
- `DocumentFile.size` is 0 on every backfilled row. Reading it means a storage
  round trip per file, and on prod that storage is BunnyCDN over the network —
  a migration making one HTTP request per document times out halfway and leaves
  the library half-converted. A management command can fill these in off the
  deploy's critical path.

---

## Phase 2 — verification models and migrations

All additive. No read site changes yet.

### On `documents.Document`

| Field | Type | Note |
| --- | --- | --- |
| `verification_status` | `CharField(choices)` | `unverified` (default) / `verified` / `changes_requested` / `hidden` |
| `verification_version` | `PositiveIntegerField(default=1)` | Bumped when files change. **Reviews key on it** — that is what makes "a new version restarts the count" and "each member reviews a version once" work. |
| `verified_at` | `DateTimeField(null=True)` | |

Do **not** reuse `Document.rating` — it is dead but it is a float and means
something else.

### New models

- **`SubjectSpecialization`** — `user`, `subject`, `source`
  (`teacher` / `earned` / `approved`), `status` (`pending` / `confirmed` /
  `declined`), `created_at`, `decided_by`, `decided_at`.
  Unique on `(user, subject)`.
- **`VerificationReview`** — `document`, `version`, `reviewer`,
  `matches_tags` / `content_accurate` / `files_complete` (all `BooleanField`),
  `note`, `outcome` (`confirmed` / `changes_requested`), `created_at`.
  **Unique on `(document, version, reviewer)`** — this is the DB-level
  expression of "each member reviews a version once". Do not rely on a view
  check alone.
- **`VerificationInvite`** — `document`, `version`, `reviewer`, `invited_at`,
  `expires_at`, `reminder_sent_at`, `state`
  (`open` / `accepted` / `expired` / `superseded`).
- **`VerificationSettings`** — a singleton, same `load()` pattern as
  `GlobalSettings`, holding the ~19 knobs in README §5a with the defaults
  given there. Keep it **separate from `GlobalSettings`**: that model already
  carries the Razorpay key, the UPI payee and the whole live-session rule set,
  and `PublicConfigView` is an allowlist precisely because of it.

### Constraints that must be enforced in the DB, not just the view

- `(document, version, reviewer)` unique on `VerificationReview`.
- `(user, subject)` unique on `SubjectSpecialization`.
- A reviewer cannot be the document's owner — a `CheckConstraint` can't span
  rows, so this one is a service-layer rule **plus** a test.

### Tests
Model-level only: state defaults, the two unique constraints, version bump
clearing nothing (the reviews stay, they just stop counting).

---

## Phase 3 — the service layer

One module, `documents/verification.py`, holding every rule so no view
re-derives them. This is the lesson from `quizzes/visibility.py` — assert
through the helper, never re-derive the rule at the call site.

- `is_specialist(user, document)` → does the user hold a **confirmed**
  specialization in any subject the document is tagged with.
- `can_review(user, document)` → specialist **and** not the owner **and** no
  review on this version **and** (if `Admin assigns`) assigned.
- `eligible_reviewers(document)` → ordered by fewest open reviews, applying
  *skip same institution*, *open reviews per reviewer*, *prefer same branch*,
  *widen to same field*.
- `record_review(...)` → writes the review, recounts, applies the transition,
  returns the new state. **Transactional, with `select_for_update()` on the
  document** — three reviewers confirming at once must not produce four
  confirmations.
- `recount(document)` → confirmations on the current version only.
- `bump_version(document)` → called when files change; resets to `unverified`.

### Tests
This is where the real coverage lives. At minimum: the threshold, the
self-review block, the one-review-per-version block, changes-requested pausing
the count, two change requests hiding the set, a new version restarting, and
editing a verified set returning it to unverified.

---

## Phase 4 — API

Mounted under the existing `/api/explore/` prefix.

```
GET    verification/queue/              reviewer's queue
GET    verification/sets/<id>/          one set for review (files + tags)
POST   verification/sets/<id>/review/   {matches_tags, content_accurate,
                                         files_complete, note}
GET    verification/me/                 my specializations + my uploads
POST   verification/specializations/    request a subject
DELETE verification/specializations/<id>/
GET    verification/notifications/
POST   verification/notifications/read/
```

Admin, under the existing admin surface:

```
GET/PATCH  verification/admin/settings/
GET        verification/admin/reviewers/        requests + reviewer list
POST       verification/admin/reviewers/<id>/{approve,decline,pause,resume}/
GET        verification/admin/attention/
POST       verification/admin/attention/<id>/{assign,restore,remove}/
```

Permissions: the admin block is **superuser or active ADMIN role** — the same
bar the research admin uses. `is_staff` alone is not enough; content-editor
staff must not reach it.

Every endpoint 404s (not 403) when `explore_verification_enabled` is off.

> **Serializer trap this codebase has hit 6+ times:** adding a field to the
> model does not put it on the API. `verification_status` must be added to
> `DocumentCardSerializer` **and** the document detail serializer explicitly,
> or the frontend will render every set as unverified and look broken.

### Notifications
Reuse `notifications/policy.py`. New verbs for: invited to review, reminder,
set verified, changes requested. **In-app + email opt-out; SMS OFF**, matching
`research.updated`. Do not add a push verb — `fcm-django` is not installed.

---

## Phase 5 — the reviewer page (`shiksha-frontend`)

`/explore/verify`, lazy route, `Verify` added to `ExploreToolbar.jsx` between
`Browse` and the right-hand group. Five tabs per README §4.

Split if the diff grows: 4a = queue + review detail, 4b = notifications + my
uploads + specializations + how it works.

The whole route is behind the flag — when off, no toolbar entry and the route
404s rather than rendering an empty page.

---

## Phase 6 — verification on the library surfaces

- `components/ui.jsx` → `DocCard` / `MiniDocCard` gain the status pill and
  progress text, and `DocCard` gains the "You specialise in X" panel.
- `DocumentPage.jsx` → the `VERIFICATION` and `HISTORY` panels (README §8).
- `ExploreBrowse.jsx` → the status filter row, including `Needs my review · N`.
- `/explore/dashboard` → the cross-link to Verify. **Nothing else.** The
  design's My Library page is a downgrade; do not port it.

Anonymous visitors see pills but no review affordances.

---

## Phase 7 — admin page (`Admin-dashboard`)

`/verification`, four tabs. The `Research papers` tab is the **existing**
`ResearchHub.jsx` — present it as a tab, do not rebuild it.

The Settings tab's summary box is **derived from the settings**, recomputed on
change, never hardcoded.

Watch the two traps already recorded for Studio screens:
- `.mod-toast` is only defined in `src/css/Moderator.css`; a screen that
  renders `<Toast>` without importing that file shows an unstyled toast on a
  hard refresh.
- Don't early-return `"Loading…"` for the whole page on refresh — it unmounts
  any open dialog. Use a quiet reload.

---

## Phase 8 — upload: multi-file and the wider file list

**Deliberately last**, because it is the only phase with an infra consequence.

1. `config/upload_validation.py` — extend `DOCUMENT_EXTS` and add a `_SIGNATURES`
   entry **per new type**. `validate_upload()` sniffs magic bytes; a type added
   to the allowlist without a signature is rejected at upload with a confusing
   error.
2. `LIBRARY_MAX_MB` is **50, not the design's 200** — raised to 200 on
   2026-11-12 and reverted by the owner on 2026-11-13. It is a separate
   constant from `DOCUMENT_EXTS`' limit so other callers are untouched either
   way. ⚠ `MAX_MB` in `UploadPage.jsx` must always equal it: if the client
   number is the larger one, a member uploads for a minute and is refused at
   the end, which reads as a random failure rather than as a rule.
3. `UploadPage.jsx:85-97` — mirror the new rules, and fix the `<input accept>`
   at `:170`, which is already narrower than the rule it advertises.
4. Confirm nginx `client_max_body_size` and the DRF/ASGI request limits still
   allow 50 MB. **Django does not limit upload size on its own** — the
   "max N MB" label is decorative until the server enforces it. Unchanged by
   this work, since the limit came back to where it was.

🔴 **Do not deploy this phase without a storage decision.** dev and prod share
one public Bunny storage zone, a pull zone serves its whole storage zone
unauthenticated, and media has no backup. The 200 MB ceiling was the part that
could not be undone after the fact, and the owner reverted it to 50 on
2026-11-13 — but the wider type list still puts video, archives and images on
that zone for the first time.

---

## Phase 9 — My Library

Replace `/explore/dashboard` with the design's single saved-sets list
(README §8).

**Remove the tabs from the screen, not the data or the routes.**
`/explore/collections`, `/explore/collections/:id` and `/explore/author/:id`
stay reachable, `ExploreStore.jsx` keeps recording reading history, follows and
likes, and no endpoint is deleted. The owner chose this knowing it drops four
tabs; keeping the data underneath is what makes the choice reversible.

---

## ⚠ Audit and remediation — 2026-11-13

An adversarial audit against README.md after the first "done" found that
**roughly a third of the spec was not implemented**, including something worse
than a gap:

### The engine was built, tested and never called

`invite_reviewers`, `expire_stale_invites`, `sync_teacher_specializations`
and `bump_version` all existed, were unit-tested, and had **zero callers**
between them (`bump_version` had one, the admin restore path). The effect:

- Nothing ever invited a reviewer, so **six of the nineteen settings were
  inert** while the admin summary box promised "The 3 least busy reviewers in
  its subjects are invited".
- Teachers were never auto-confirmed, so "Teachers review in the subjects they
  teach, with no extra step" did nothing.
- Two of the six transition rules could not fire; the "Upload a new version"
  button created a **second, separate document** and left the rejected set
  frozen forever.
- Two of the four notification kinds could never appear.

🔴 **Every test passed throughout, because each test called the function
directly.** A test that invokes `invite_reviewers()` proves nothing about
whether uploading invites anybody. `documents/tests_verification_api.py`'s
`EngineWiringTest` now asserts the CALLERS — upload, task, signal — not the
functions.

🔴 **And the first fix for the teacher hook was itself silently dead.** The
receiver was a function defined *inside* its connector, and Django holds
receivers by **weak reference** — so it was garbage-collected the moment the
connector returned. Nothing errored; the signal simply had no listeners. It is
now a module-level function connected with `weak=False`. See the comment in
`documents/verification_signals.py`.

### Fixed in the same pass

| Was | Now |
| --- | --- |
| No invites on upload | `verification_signals.invite_for()` from `views.py` after the tags exist |
| `expire_stale_invites` uncalled | `documents.expire_verification_invites`, hourly |
| No reminders | `documents.send_verification_reminders`, daily |
| Teachers never confirmed | `post_save` on `TeachingAssignment` + a daily backstop task |
| No new-version flow | `POST verification/sets/<id>/version/` + a real button |
| "Mark all as read" forged `reminder_sent_at` | its own `read_at` column (`0013`) |
| Upload form was the old wizard | three numbered sections, `TagPicker`, Ready-to-publish |
| One tag path per set via the UI | several — the design's two-field example is creatable |
| Comma-separated Tags field | removed, as the design specifies |
| No browse panel | `FieldBrowser`, three chip rows with counts, served by `_field_tree()` |
| No file-type row | `FILE_KINDS` / `FILE_KIND_EXTS` + `?filekind=` |
| Document page had no FILES/TAGS | both panels; a set's extra files are reachable |
| `N files: DOCX, XLSX` missing | on the card when > 1 file |
| Specializations said "2" always | reads `verified_uploads_to_earn_it` |
| How-it-works rule said "2" always | reads `change_requests_before_hidden` |
| "Confirmations paused at 1 of 3 confirmations" | "…at 1 of 3" |
| "Approved by an administrator" | "Approved by an admin" |

### Still not done, deliberately

- **Fields are eight, not the design's six.** `Health` and `Other` carry the
  subjects that fit none of the six; see `fields.py`'s docstring.
- **Upload size is 50 MB, not 200** — reverted by the owner 2026-11-13.
- `DocumentFile.size` is 0 on backfilled rows.
- The static branch tree and live uploader-invented branches can differ by
  wording ("Geotechnical Engineering" vs "Geotechnical") and both render. That
  is inherent to branches being free text with no approval, which the design
  requires; force-merging near-matches would silently relabel what someone
  typed.

---

## Status — ALL PHASES DONE, AUDITED AND REMEDIATED 2026-11-13

Nothing is committed. Nothing is deployed. The flag ships **OFF**.

### Tests

```
documents notifications global_settings accounts courses forum
  = 960 tests OK          (baseline for the same apps: 880 — +80 new, 0 regressions)
documents alone           = 303 tests OK
shiksha-frontend   npx vite build = green; browseFilters.test.mjs 4/4
Admin-dashboard    npx vite build = green  (use npx; `npm run build` fails by design)
```

### Browser-verified on a local stack

`ev-backend-8040` + `ev-frontend-5210` + `ev-admin-5211` in `.claude/launch.json`,
against `/tmp/claude-1000/ev_verify.sqlite3`. These configs carry their own
`TEST_DB_PATH` on purpose — the workspace's `backend-local` has none and lands
on an empty database.

Driven for real, not just rendered:

- A reviewer answered the three checks and confirmed a set. The queue went
  3 → 2, the card moved to "1 of 3 confirmations", and the review is in the
  database with the right subject recorded.
- **Multi-tag matching**: the same browse page told one viewer "You specialise
  in Sociology" on one card and "You specialise in Civil Engineering" on
  another, from the same account. That is the whole reason tags are a table.
- **`Needs my review`** returned 2, correctly excluding the set already reviewed.
- An admin changed "Confirmations needed" to 4; the summary box kept saying
  "After 3" with an explicit "this describes the saved settings" warning until
  Save, then said "After 4". The derivation is real.
- A pending request was approved and the member appeared in the reviewers list.
- Two change requests hid a set; it appeared under Needs attention with **both**
  reviewer notes; Restore put it back as Unverified at **version 2** — so the
  change requests stop counting and it cannot immediately re-hide — and both
  reviews survived as history.
- **Flag OFF**: no Verify entry, no status row, no badges, all documents still
  listed. Explore is byte-for-byte what it was before this feature existed.

### Known gaps, deliberate

- `DocumentFile.size` is 0 on backfilled rows. Filling it means one storage
  round trip per file, and on prod that storage is BunnyCDN over the network —
  a migration doing that times out halfway and leaves the library
  half-converted. A management command can do it off the deploy's critical path.
  The UI already hides the size when it is 0.
- `Download all` on the document page is replaced by **Open all N files** in
  the FILES panel. The design's button did nothing; a browser-blocked
  multi-tab open is honest about what it does, where a silent no-op was not.
- `Document.file` is still what the reader and the download endpoint address;
  `DocumentFile` is what the FILES panel and every multi-file path use. Both
  columns are kept in sync — see the deferred step below.
- The static branch tree and uploader-invented branches can differ by wording
  ("Geotechnical Engineering" vs "Geotechnical") and both render as chips.
  Inherent to branches being free text with no approval, which the design
  requires; force-merging near-matches would relabel what someone typed.

### 🔴 Before deploying

1. **The wider file types are the risky part, not the verification.** dev and
   prod share one public Bunny storage zone, served unauthenticated with no
   backup, and this puts video, archives and images on it for the first time.
   The 200 MB ceiling has been reverted to 50 (owner, 2026-11-13), which caps
   the damage but does not change what kinds of file now land there.
2. **Check nginx `client_max_body_size` still allows 50 MB.** Django does not
   limit upload size on its own — the label is decorative until the server
   enforces it. Unchanged by this work, but worth confirming.
3. Run `0011` (the backfill) on a copy first and check the row counts. It is
   the only migration here that touches existing data.
4. Flipping the flag needs a **re-login** in `Admin-dashboard`.

---

## Deferred — dropping the superseded columns

After Phase 8 ships and has run for a release: drop `Document.file`,
`filetype`, `pages`, `file_hash`, `subject`, `topic`.

**Grep every app, not just `documents/`.** The precedent is `is_active`, whose
last read site was in `courses/views.py` — grepping the owning app alone missed
it and the public featured endpoint broke.

---

## Not in scope

- The `Research papers` admin tab (already built — present the existing
  `ResearchHub.jsx` as a tab, do not rebuild it).
- Appeals, cancelling a correction and the signed-out Research view — the
  design project lists these as unbuilt.
- The five older design pages (Dashboard Home, Skill Dev Home, Skill Public,
  Academy Lists, Shiksha Dashboards).
- The design's dead affordances: `Download all`, the file `Open` / `Preview`
  buttons, `Courses` and `About` in the header, and `Reset demo data`. Build
  them properly or leave them out — do not ship them inert.
