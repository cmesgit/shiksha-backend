# RC-031 evidence — room-file expiry, attachment safety and quota

Date: 17 November 2026. Branch: `docs/shiksha-remote-plan-on-dev` (worktree
`~/workspace/projects/worktrees/remote-be`). **Pulled forward out of Phase 5**
— its only dependencies are RC-001 and RC-005, both met, and it closes
exposure that is live today rather than guarding future companion work.

**No migration. No new model. No new route.** Four existing files changed plus
one new test module.

## The finding that justified the task

`SessionFile.expires_at` and `PrivateSessionFile.expires_at` were
**write-only**. Set at upload by `live_files_views`, and read by exactly one
consumer — `sessions_app/management/commands/purge_expired_session_files.py`.

🔴 **That command is scheduled nowhere.** It is absent from
`config/celery.py`'s 16 beat entries, has no wrapper in
`config/maintenance_tasks.py` (which wraps three other maintenance commands
for exactly this reason, and whose docstring records that these commands
"were never actually scheduled anywhere"), and no crontab or systemd timer in
the repo creates it. Its own docstring suggests `*/15 * * * *` — a comment,
not a configuration.

So the module docstring's promise that room files "self-destruct after
`GlobalSettings.live_file_retention_days`" (default **2 days**) was enforced
by nothing on any path:

| Path | Before |
|---|---|
| `GET …/files/` listing | returned expired rows, with live URLs |
| `GET /api/media/secure/…` | served the bytes; `_check_session_file` never mentioned `expires_at` |
| session-summary `files[]` | returned expired rows |
| sweeper | never runs automatically |

The contrast was already in the same file: `_check_chat_attachment`
(`config/media_security.py:255`) *does* fail an expired row, with a comment
calling it defence-in-depth against "a race between the expiry sweep and an
in-flight request". Session files had no equivalent.

Why the existing tests missed it: `config/tests_media_security.py`'s two
session-file fixtures are both created `expires_at=now + 7 days` — deliberately
unexpired — and every assertion calls `is_authorized()` directly rather than
fetching anything.

## Delivered

| File | Change |
|---|---|
| `sessions_app/live_files_views.py` | `file_is_expired` / `live_files` / `can_read_group_file` / `can_read_private_file` policy helpers; expiry-filtered listings; atomic quota; `_safe_original_name` / `_safe_content_type` |
| `config/media_security.py` | `_check_session_file` dispatches to those helpers instead of restating the rule; blanket `is_staff` bypass removed |
| `config/media_views.py` | inline-type allowlist, `Content-Disposition`, explicit `X-Content-Type-Options: nosniff` |
| `config/tests_media_security.py` | one assertion inverted (see below) |
| `sessions_app/tests_room_file_hardening.py` | **new, 29 tests** |

Authorization for room files now has **one** definition. `media_security`
already imported `_in_room` from the views module rather than duplicating it;
this extends that existing arrangement rather than introducing a new one, so
the bytes and the JSON listing cannot drift apart.

## Two deliberate behaviour changes

**1. Staff no longer read `PrivateSessionFile`s.** The old
`_check_session_file` returned `True` for any `is_staff` account before it
looked at the model at all. A `PrivateSession` is 1:1 tutoring and its files
are recipient-only; RC-031's brief is explicit — "without granting
private-recipient access to staff".

Compatibility checked, not assumed: `grep -rn PrivateSessionFile --include=*.py`
over the whole repo returns the model, the views, the purge command, the
migration and tests. **No admin view, export, report or serializer consumes
it.** This narrows an unused capability rather than breaking a workflow.
Staff review access to *group room* files is deliberately untouched, matching
`_check_chat_attachment` and `_check_forum_attachment`.

`config/tests_media_security.py::test_staff_can_read_either` was therefore
split into `test_staff_can_read_a_group_room_file` (unchanged behaviour) and
`test_staff_cannot_read_a_private_session_file` (`assertTrue` → `assertFalse`).
**That is the only existing assertion this task inverts.**

**2. Expiry binds every role, staff included.** Retention is a promise to the
people in the room, so no role can still fetch a file the product told a user
had self-destructed. This differs from `_check_chat_attachment`, where staff
bypass expiry for moderation review — a deliberate divergence, because a room
file's retention window is user-facing copy and a chat attachment's is not.

## Attachment safety — note this is repo-wide, not room-file-only

`secure_media_view` serves **every** private media path, so the new headers
apply to all of them. The inline set is an allowlist (images, PDF, common
A/V, `text/plain`); everything else, including anything unrecognised, becomes
`application/octet-stream` + `Content-Disposition: attachment`.

`image/svg+xml` is **deliberately excluded** despite being an image — SVG
carries script, and this content is user-uploaded and served from the API's
own origin. Public media is unaffected: public paths resolve to `/media/…`
and nginx serves them directly without reaching this view.

The filename offered is the *stored* basename, not `original_name` — this
view is generic across every media path and has no model to look that up in.
RFC 5987 percent-encoded, so a filename can never break out of the header.

`X-Content-Type-Options` is set explicitly rather than left to
`SecurityMiddleware`, because on the `X-Accel-Redirect` path nginx builds the
body-carrying response from this header set. A test asserts the header is
present on **both** branches.

## Quota

The cap was a plain read-then-write (`count()` then `create()`), so N
concurrent uploads all read `count == limit-1`, all pass and all insert.
Both views now take `select_for_update()` on the parent session row inside
`transaction.atomic()`.

🔴 **The test suite cannot prove this lock.** On SQLite `select_for_update()`
is a silent no-op — deleting the lock fails nothing locally. It is
load-bearing only on the real PG 16. This is the same gap RC-008 has to close
for the two-room approval race; **whoever sets up that PG-16 harness should
add this case to it.** What the tests *do* prove is the cap's behaviour
(409 `too_many`) and that expired rows no longer occupy it.

Expired rows stop counting toward the quota deliberately: they are gone from
every read path, so letting them hold slots would permanently wedge uploads
in any room older than the retention window. Consequence to be honest about:
**with the sweeper still unscheduled, disk use is not bounded by the quota.**

## Tests

    cd ~/workspace/projects/worktrees/remote-be
    export TEST_DB_PATH=/tmp/claude-1000/rc031.sqlite3 \
           DJANGO_SETTINGS_MODULE=config.settings_test
    .venv/bin/python manage.py test sessions_app.tests_room_file_hardening \
                                    config.tests_media_security
    .venv/bin/python manage.py test sessions_app livestream
    .venv/bin/python manage.py test config

| Suite | Result |
|---|---|
| `sessions_app.tests_room_file_hardening` | **29 tests, OK** |
| `config` (incl. `tests_media_security`) | **29 tests, OK** |
| `sessions_app livestream` | **293 tests, 4 failures + 1 error** |
| `makemigrations --check --dry-run` | **No changes detected** |

🔴 **The assertion that matters is that the failure count did not move.**
`sessions_app livestream` went **264 → 293 tests (+29)** and stayed at
**4 failures + 1 error** — the same five environmental LiveKit cases by name,
unchanged from the 17 November baseline:
`test_request_creates_participant`, `test_request_with_group_students`,
`test_student_can_request_session` (all `RequestSessionTest`),
`test_participant_can_view` (`SessionDetailTest`), `test_start_session`
(`SessionLifecycleTest`). All in `sessions_app.tests`. Do not fix them and do
not let the count move.

## Mutation-checked, because trap #17 says to

Every negative assertion was re-run against deliberately broken code to
confirm it fails for the reason claimed:

| Mutation | Expected | Observed |
|---|---|---|
| `file_is_expired()` → `return False` | the 4 expiry-download tests fail | **4 failed**, rest green |
| restore the `is_staff` bypass in `can_read_private_file` | both staff-private tests fail | **2 failed** (one in each module) |
| revert `_safe_original_name` / `_safe_content_type` to the old slicing | the sanitisation tests fail | **3 failed** |

🔴 **This caught a real vacuous test, exactly as the trap predicts.** The
first draft asserted through the endpoint that `"../../etc/passwd"` is
reduced to `"passwd"` and that `"C:\Users\me\x.pdf"` loses its path. Both
passed **with the sanitiser deleted** — Django's `MultiPartParser` already
does that, and strips NUL bytes too, before a view ever sees `upload.name`.
Verified by driving a raw multipart body through the parser directly.

Those cases were demoted to an explicitly-labelled unit test (the sanitiser
is still the only guarantee for a non-multipart caller or a future parser
change) and the endpoint tests now assert the **actual delta**: Django leaves
`"...."` and `"  spaced.pdf  "` untouched. A fourth draft test
(content-type parameters) was **deleted** rather than kept, having been found
vacuous for the same reason — Django already splits them off.

## Not done — deliberately out of the named file list

1. 🔴 **The sweeper is still unscheduled.** Read-time enforcement means the
   bytes are unreachable, but they are never deleted, so this is now a
   retention/disk problem rather than a disclosure one. The fix is a
   `config/maintenance_tasks.py` wrapper plus a `config/celery.py` beat
   entry — **neither file is in RC-031's modify list**, and OD-02 already
   records that "the platform purges nothing today". Recommend folding it
   into RC-030, which owns real deletion with behavioural acceptance.
2. **`sessions_app/group_session_views.py:2219` still lists expired rows** in
   the session-summary response (its `# ── files (with expiry) ──` comment
   refers to including the `expires_at` field, not filtering on it). Not an
   exposure any more — those URLs now 404 — but it is a visible inconsistency
   with the files endpoint. One line, using `live_files()`; the file is not
   in the modify list, so it needs a contract-owner nod.
3. **No aggregate byte quota**, and no magic-byte content validation.
   `config/upload_validation.py` exists and is used by `documents/` and
   `accounts/`; wiring it in here would change which uploads are *accepted*,
   which is a product decision, not hardening.
4. **Bunny**: if `BUNNY_STORAGE_ZONE` + `BUNNY_STORAGE_API_KEY` are ever set,
   `BunnyStorage.url` returns a public unauthenticated CDN URL and this
   entire module is bypassed for room files. That is the known standing
   exposure, not something RC-031 introduces or can fix — see RC-019's
   blocker.

## Rollback

`git revert` of the single commit. No migration, no data change, nothing to
undo on a box. The only user-visible reversions would be expired files
becoming downloadable again and staff regaining private-session file access.

## Next integration task

Unchanged: **RC-006** (participant bindings + CSRF boundary) remains the next
Track A task. RC-031 unblocks **RC-019**, which inherits the quarantine-storage
requirement above.
