# shiksha-backend — session context

## Known pre-existing test failures (not regressions — do not "fix" blindly)

Running `python manage.py test` (settings: `config.settings_test`, sqlite +
LocMemCache, no real Redis/LiveKit) currently reports **6 failures + 6
errors, all in `chat` and `sessions_app`**, unrelated to any other app. Root
causes, confirmed by reading the actual tracebacks on 2026-08-07:

- **`chat.tests.test_m0_regression` (RateLimitTest, UnreadCounterTest) and
  `chat.tests.test_m3_consumer_integration` / `test_realtime_stage_bcd` /
  `test_migration_0006`** — `redis.exceptions.ConnectionError: Error 111
  connecting to 127.0.0.1:6379. Connection refused`. These tests call
  `chat/redis_utils.py`'s raw Redis client directly, which bypasses the
  in-memory cache `settings_test.py` substitutes for `CACHES`. There is no
  Redis server running in this dev sandbox.
  **To actually fix/run these**: start a local Redis (`docker run -p
  6379:6379 redis` or similar) before running the suite, or the chat app's
  test setup needs a fixture that mocks `redis_utils.get_redis()` when no
  real Redis is reachable.

- **`sessions_app.tests` (RequestSessionTest, SessionDetailTest,
  SessionLifecycleTest)** — 400/403 responses where 201/200 were expected,
  and a missing `room_name` key. Consistent with missing LiveKit credentials
  (`LIVEKIT_URL`/`LIVEKIT_API_KEY`) in `settings_test.py` — session
  creation/start calls out to LiveKit room provisioning, which fails
  silently into a 400/403 without those set.
  **To actually fix/run these**: set real (or sandbox) LiveKit credentials
  in the environment before running the suite, or add a fake/stub LiveKit
  client for tests.

Neither issue touches `scholarship`, `enrollments`, `courses`, `accounts`,
or `global_settings` — all of those pass cleanly. Before spending time on
either fix, confirm they still reproduce (Redis/LiveKit reachability is an
environment fact, not a code fact, and may already differ by the time this
is read).

## Instant Scholarship module — status (2026-08-07)

Full design handoff (11 screens, design tokens, business rules) lives at
`~/Downloads/Shiksha Instant Scholarship Module/design_handoff_instant_scholarship/README.md`.

**Backend: done, tested, uncommitted** (branch `dev` — not committed or
pushed; ask before doing either). New app: `scholarship/` — 9 models
(`ScholarshipSettings` singleton, `ScholarshipBand`,
`ScholarshipQuestionBankItem`, `GuardianVerification`,
`ScholarshipEligibilityRecord`, `ExamSession`, `ExamQuestion`, `ExamAnswer`,
`CheatSignalEvent`, `ScholarshipAward`), full student + admin API, Django
admin registrations, Celery deadline sweep. Mounted at
`/api/scholarship/`. 14/14 own tests pass; full suite otherwise clean (see
above). Also touched: `config/settings_base.py` (app registration,
`SCHOLARSHIP_DEDUP_PEPPER`, throttle scope), `config/urls.py`,
`config/celery.py` (2 beat entries), `enrollments/payment_views.py`
(additive, defensive scholarship-award redemption hook in `FreeEnrollView`).

Key design choices worth knowing before touching this code:
- Identity is anchored on the **parent/guardian**, never the student
  directly (DPDP Act 2023 §9 + Aadhaar law reasons — see
  `scholarship/models.py`'s `GuardianVerification` docstring for the full
  citation trail). `ShikshaCom` must never store an Aadhaar number or a
  hash of it — only a licensed KYC reseller's opaque, non-reversible token.
- Eligibility is one attempt per verified person per **academic year**
  (not per class), enforced by a DB `UniqueConstraint`, and the check is
  **idempotent** (`services.get_or_reserve_eligibility`) so reloading the
  instructions screen mid-flow doesn't falsely report "already attempted."
- The exam deadline is enforced **lazily on every read/write**
  (`services.expire_if_past_deadline`), not by the Celery sweep alone — the
  sweep is only a backstop for abandoned sessions nobody ever reloads.
- No real KYC vendor is wired yet. `GuardianVerificationCreateView` creates
  a `pending` record for DigiLocker/Aadhaar OTP but there's no callback
  handler — same "documented stub" pattern as
  `enrollments/payments.py`'s `RazorpayProvider`. Only `manual` document
  review can reach `verified` today, via the admin action endpoint.
- No phone-OTP fast-fail dedup layer exists — flagged as a deliberate scope
  cut (no SMS-OTP infra in this codebase yet), not an oversight.

**Student-facing frontend: done, browser-verified end to end (2026-08-07).**
Built entirely in `shiksha-frontend/src/scholarship/` (11 screens: Landing,
CourseSelect, Verify, Details, Eligibility, Instructions, Exam, Evaluating,
Result, Checkout, Confirmation) — confirmed `shiksha-frontend` is the
correct home (public marketing + funnel patterns already exist there via
`src/counselling/`; `shiksha-student-dashboard` hard-redirects every
unauthenticated visitor out, so it can't host any of this). Routes wired
into `App.jsx` under `/scholarship/*`; identity verification onward is
`ProtectedRoute`-gated (the `?next=`/`post_auth_redirect`/`LoginRedirect`
pattern already in this codebase handles "log in mid-flow, return to where
you were"). Checkout calls the real `freeEnroll()`/`getPaymentConfig()`
from `api/enrollments.js` — a scholarship redemption produces a real
`Enrollment`+`Subscription` through the exact same pipeline a paying
student uses, not a parallel one.

Two small additive backend endpoints were added to support this:
`GET /api/scholarship/exam/session/current/` (resume-banner lookup — "is
there a live session for me" without already knowing its id) and
`GET /api/scholarship/config/` (safe-to-show subset of `ScholarshipSettings`
+ band table, so the calculator/instructions/exam screens reflect real
admin config instead of hardcoded numbers). `courses/views.py`'s public
catalog gained one field, `class_level`, needed to filter/display
scholarship-eligible courses. `ExamResultSerializer` gained `award_id`.

**Departs from the design prototype on purpose, honestly**: the mockup
shows identity verification completing in ~3 fake seconds via 4 scripted
ticks. Since no real KYC vendor is wired (see above), that would mean
claiming "Identity confirmed" for a verification that never actually
happened — the frontend instead submits for real and polls the real status
(`GuardianVerificationStatusView`), so it becomes instant automatically the
moment a vendor is wired, with zero rewrite. Same principle for
"Evaluating": dwell time is whatever the real submit call takes (~400ms
floor to avoid a jarring flash), not a padded fixed delay.

**Verified live** (local dev: `backend-local` launch config on
`config.settings_test` + sqlite, `frontend-dev` pointed at it via a new
`shiksha-frontend/.env.local` — gitignored, not committed): full click-
through course→verify→details→eligibility→instructions→exam→submit
→result→checkout→confirmation; per-student shuffled options confirmed (the
answer key never reaches the client — checked via
`ExamQuestionStudentSerializer` output); tab-switch detection posts a real
cheat-signal event; autosave PATCHes land; palette navigation and the
server-driven countdown work; resume banner correctly finds a live session
on a different profile and resumes into the exact same in-progress exam;
siblings under one verified parent each get their own eligibility record
(dedup correctly keyed on child name+DOB, not just the parent); a winning
session (45/50 → 40%) redeems into a real `Enrollment`+`Subscription`,
confirmed by direct DB query, not just a UI success message.

**Two real bugs found and fixed during this verification** (both in
frontend code, not caught by any automated test since they only surface
against a real API response): (1) `AuthContext`'s `activeProfile` is the
full serialized profile-card object, not a bare id — a hook that used it
directly as a URL path segment silently 404'd. (2) `ProfileDetailView`'s
PATCH endpoint accepts `first_name`/`last_name`, not `full_name` — a form
that PATCHed `full_name` was silently dropped by the API with no error.
Worth remembering when writing more frontend code against this backend.

**Known, accepted gap**: clicking a course card / exam option div doesn't
show up in the accessibility tree (no `role="button"`/`tabIndex`) — matches
the original design prototype's own gap (`README.md`'s Open Items #6 lists
a full accessibility audit as explicitly out of scope for that pass too).
Not fixed here; flag before shipping to production.

**Free, legal, WORKING Aadhaar verification shipped (2026-08-07)** —
`scholarship/aadhaar_offline.py`. Verifies UIDAI's Aadhaar Paperless
Offline e-KYC (the resident-downloaded, share-code-protected ZIP) against
UIDAI's own published signing certificate
(`scholarship/certs/uidai_offline_publickey_26022019.cer`, independently
re-verified via `openssl` against a fresh download, not just trusted from
an agent's report). No AUA/KUA licence, no paid reseller — genuinely free
and legally clean. New verification method
`GuardianVerification.METHOD_AADHAAR_OFFLINE`, new setting
`allow_aadhaar_offline` (default True); `allow_aadhaar_otp` (the paid-
vendor stub) changed default to **False** since turning it on would offer
a method that can never complete. `POST /api/scholarship/verification/`
now accepts `{method: "aadhaar_offline", ekyc_zip: <file>, share_code}` and
verifies **synchronously** — no vendor callback to wait for.

Read the full compliance/security reasoning in `aadhaar_offline.py`'s
module docstring before touching it — it is dense and load-bearing.
Highlights:
- **Never store the Aadhaar number in any form.** The XML's own
  `referenceId` embeds the last 4 digits + a timestamp; `dedup_reference_for()`
  deliberately excludes it, hashing only verified name+DOB+gender.
- **The pinned UIDAI certificate is expired** (not after 9 Apr 2019) and has
  been for years — independently confirmed, not a bug in this code. Only
  the RSA signature is checked; X.509 chain/expiry validation is
  deliberately skipped since it would always fail against this artifact.
- **No official UIDAI reference implementation exists in any language.**
  This was built from UIDAI's own sample-data page + independent cert
  verification — test against a REAL downloaded e-KYC document before
  relying on this in production. The test suite (`AadhaarOfflineModuleTest`,
  `AadhaarOfflineViewTest` in `scholarship/tests.py`) proves the **reject**
  direction thoroughly (forged signature, wrong share code via a genuinely
  zip-encrypted fixture built with the system `zip` binary, stale document,
  malformed input — 12 tests, all passing) but CANNOT prove the accept
  path, since a genuine UIDAI-signed document can only come from a real
  Aadhaar holder completing UIDAI's own OTP flow.
- New dependencies: `signxml`, `defusedxml`, `lxml` (added to
  `requirements.txt`, already `pip install`ed into `.venv`) — `defusedxml`
  specifically to avoid XXE on this user-supplied XML; UIDAI's live
  signature algorithm was uncertain (their own sample uses deprecated
  RSA-SHA1) so the code tries the secure default method set first and
  falls back to explicitly allowing RSA-SHA1 only if the document declares
  it, rather than silently downgrading security across the board.
- Frontend: `Verify.jsx` now reads `verification_methods` from
  `GET /api/scholarship/config/` (new field) rather than hardcoding which
  methods to show — an admin can toggle any method without a frontend
  deploy. Browser-verified: the method list correctly reflects server
  config (Aadhaar OTP hidden, Offline e-KYC shown), the new upload UI
  renders with a real link to UIDAI's actual "how to generate" FAQ page,
  and client-side validation blocks submission with no file/share-code
  before any request fires. The actual file-upload interaction couldn't be
  driven through browser automation (no OS file-picker capability in this
  tool) — the backend tests exercise the identical multipart endpoint with
  a real (forged, for obvious reasons) zip instead, which is equivalent
  fidelity for what matters here (does the server-side verification
  actually reject correctly).

**Admin-dashboard UI: done, browser-verified against the real backend
(2026-08-07).** Built in `Admin-dashboard/src/pages/scholarship/` — one nav
entry ("Instant Scholarship", with a live badge summing
`flagged_for_review_open` + `pending_verifications`) leading to an 8-tab
panel (`ScholarshipPanel.jsx`): Overview/stats, Settings, Bands, Question
Bank (+ AI-generate/bulk-create review flow), Verifications, Sessions
(with cheat-signal detail + clear/void), Eligibility, Awards. Mirrors this
app's own existing patterns exactly (`SkillCMSPanel.jsx` for the tab shell,
`skillcms/Categories.jsx` for CRUD+modal, `AcademyQuizzes.jsx` for the
review-queue+detail-modal shape) — nothing novel invented, all API calls
in a new `src/api/admin_scholarship.js` file per this app's per-feature-file
convention.

One real backend gap found and fixed while building this: `AwardListView`
was reusing the *student-facing* `ScholarshipAwardSerializer`, which
deliberately has no `learner_profile` field (a student only ever sees
their own award) — useless for an admin list that needs to know WHICH
student an award belongs to. Added a proper `ScholarshipAwardAdminSerializer`
with `learner_name`/`course_title` and swapped it into both `AwardListView`
and `AwardVoidView`.

**Verified end-to-end against the real local backend** (not just rendered
in isolation): settings edit persisted and was re-read from a fresh DB
query; all 7 seeded bands listed; all ~300 seeded question-bank rows
listed and filter-by-subject correctly re-queried the server; the
AI-generate flow correctly surfaced the real backend error
("OPENAI_API_KEY is not configured…") rather than failing silently; the 1
real guardian verification, all 3 real exam sessions (including the one
real cheat-signal event from earlier flow testing), all 3 real eligibility
records, and the 1 real redeemed award (learner name now correctly
showing after the serializer fix) all displayed accurately; clicking
"Clear flag" on a flagged-adjacent session issued a real PATCH that
persisted (`review_status=cleared`, `reviewed_by=admin@example.com`,
confirmed by direct DB query).

**Not started**: nothing structurally — the module (backend + student
frontend + admin UI + free/legal Aadhaar verification) is functionally
complete end to end. Remaining future work is optional: a real KYC vendor
for DigiLocker/Aadhaar-OTP instant UX, and an accessibility pass.

## Automatic class recording (LiveKit Egress → Bunny) — PHASE 0 ONLY (2026-08-28)

Branch `claude/livekit-egress-bunny-storage-f23q5y`. **Phase 0 is groundwork
only: no egress is ever started yet, and nothing in this phase changes
runtime behaviour.** The feasibility verdict that deferred this as "HEAVY
infra" is out of date — Bunny Storage's S3-compatible API went GA
2026-07-09, and LiveKit Cloud runs egress as a managed service, so no relay
box and no CPU on the prod droplet is involved.

**The shape of the whole feature**, so phase 0's choices make sense: egress
can write to Bunny *Storage* but not Bunny *Stream*, and Stream is what the
entire existing playback path speaks (`SessionRecording.bunny_video_id`, the
0–5 status codes, `config/bunny_signing.py`'s token embed). So it hops once:
egress → Storage → `POST /library/{id}/videos/{guid}/fetch` → Stream →
existing polling and signed embed, untouched.

Landed in phase 0:
- `config/settings_base.py` — a `BUNNY_EGRESS_*` block plus
  `LIVEKIT_EGRESS_ENABLED`. Read that block's comments before touching it;
  the credential separation is a safety property, not tidiness.
- `courses/models_recordings.py` — `SessionRecording.uploaded_by` is now
  nullable `SET_NULL` (was non-null `CASCADE`). An egress recording has no
  human uploader. All four existing readers were checked and were already
  None-safe; `courses/tests_recordings.py::UnownedRecordingTest` pins them.
- `livestream/models.py` — new `LiveSessionEgress`, one row per *attempt*.
  Its `status` tracks ONLY LiveKit's egress state machine; the
  post-processing state (fetched? transcoded? raw file purged?) is
  deliberately derived, not a second status column. See the class docstring.
- Migrations `courses/0039`, `livestream/0011`; read-only admin registration.

Traps for whoever does phases 1–5 (each already cost something to find):
- **`BUNNY_EGRESS_S3_HOST` is not `BUNNY_STORAGE_HOSTNAME`.** Bunny's S3 API
  answers on `<region>-s3.storage.bunnycdn.com`; the native Edge Storage API
  that `config/bunny_storage.py` uses is `storage.bunnycdn.com`. Pointing
  egress at the native host fails to authenticate. Verified against Bunny's
  own docs after getting it wrong from memory first.
- **Start egress on teacher-join, not `room_started`.** `livestream/views.py`
  (see the comment in `_handle_room_started`) spells out that `room_started`
  fires when *any* participant connects, including a student arriving before
  the teacher. Recording an empty room is billed egress minutes.
- **Egress webhook events carry no `event.room`.** `_event_room_name()` and
  `_event_dedupe_id()` in `livestream/views.py` both read `event.room.name`;
  for egress events the room name lives on `event.egress_info`. Left alone,
  every egress event logs with `room_name=""` and `session=None`.
- **`POST /videos/fetch` cannot read a signed URL**, so the raw mp4 must be
  briefly public on a pull zone. That is why object keys carry a random
  segment and why the egress zone needs its own pull zone.
- **Do not rehearse against the CMS storage zone** — it is shared between dev
  and prod. `settings_base` now raises `ImproperlyConfigured` if
  `BUNNY_EGRESS_ZONE == BUNNY_STORAGE_ZONE`, so this trap is closed
  structurally rather than by remembering it.
- Phase 4 needs a Celery beat task; `CheckVideoStatusView` is client-polled
  only and no video-related Celery task exists. Extract
  `courses/views_recordings.py`'s Bunny status/duration/thumbnail block into
  a shared function rather than copying it — it carries a real bug fix.

## Local environment note: the Dockerfile's Python is too old for its own requirements

`Dockerfile` pins `python:3.11-slim`, but `requirements.txt` pins
`Django==6.0.1`, which requires Python >= 3.12 — so `pip install -r
requirements.txt` fails outright on 3.11 and that image cannot build as
written. Not fixed here (out of scope, and untested against the real deploy);
locally, build the venv with `python3.12` or newer.
