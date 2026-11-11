# RC-001 evidence — existing meeting compatibility baseline

Run date: 11 November 2026, Asia/Kolkata. Local developer machine, no network, no production connection, no deployment.

## Environment

| Item | Value |
|---|---|
| Interpreter | `shiksha-backend/.venv/bin/python` — CPython 3.12.3 |
| Django | 6.0.9 (`requirements.txt` pin) |
| Settings | `config.settings_test` (SQLite, LocMemCache, InMemoryChannelLayer, `CELERY_TASK_ALWAYS_EAGER`) |
| LiveKit | none; tests supply throwaway credentials via `override_settings` |

## What was added

`shiksha-backend/sessions_app/tests_collaboration_baseline.py` — 8 tests, no production code, no migration, no generated shared copy touched.

| Test | Pins |
|---|---|
| `test_two_unrelated_students_can_meet_without_enrollment` | Two STUDENT accounts create and join an instant room with `Enrollment.objects.count() == 0` and `session.subject is None` |
| `test_two_unrelated_teachers_can_meet` | Same for two TEACHER accounts |
| `test_direction_is_symmetric` | B-hosts-A is the same contract as A-hosts-B; distinct rooms |
| `test_teacher_student_pair_uses_the_identical_path` | Mixed pair takes the same code path, no pairing record |
| `test_anonymous_cannot_create_or_join` | Anonymous create and join denied, no token in the body |
| `test_lobby_arrival_gets_no_token_until_admitted` | `admit_mode="lobby"` non-host gets `202 {status: pending}` and **no token**; token only after the host admits through the real endpoint |
| `test_terminal_sessions_issue_no_token` | `completed` / `cancelled` / `expired` each return 400 with no token |
| `test_instant_meetings_default_to_the_lobby_gate` | The shipped `admit_mode` default for instant meetings is `"lobby"` — the one deliberately branch-dependent assertion in the file |
| `test_join_does_not_invent_a_room_name` | A client-supplied `room` in the join body is ignored; server room name wins in both the body and the signed grant |

Every successful join additionally asserts the full response key set, the LiveKit identity `<user_uuid>_<session_uuid>`, the room `group_session_<session_uuid>`, and `video.roomJoin` / `video.room` decoded from the real signed JWT.

## Results

Against `origin/main` and `origin/dev` (the branches this work targets), in clean worktrees:

```
manage.py test sessions_app.tests_collaboration_baseline --settings=config.settings_test
origin/main   Ran 9 tests — OK
origin/dev    Ran 9 tests — OK
```

Against the local working branch `fix/bunny-purge-on-delete`, which is 17 commits behind `origin/main`:

```
.venv/bin/python manage.py test sessions_app.tests_collaboration_baseline --settings=config.settings_test
Ran 8 tests — OK  (pre-correction suite; see mismatch 0)

.venv/bin/python manage.py test sessions_app.tests_group_session_socket sessions_app.tests_group_session_end --settings=config.settings_test
Ran 26 tests — OK

.venv/bin/python manage.py test sessions_app livestream --settings=config.settings_test
Ran 226 tests — FAILED (failures=4, errors=1)
```

The 226-test run's five non-passes are all in `sessions_app.tests` — `RequestSessionTest.test_request_creates_participant` (error), `test_request_with_group_students`, `test_student_can_request_session`, `SessionDetailTest.test_participant_can_view`, `SessionLifecycleTest.test_start_session`. These are the **pre-existing** missing-LiveKit-credential failures recorded in the workspace `CLAUDE.md` ("4 failures + 1 error, all `sessions_app`. Environmental — do not fix, do not let the count change"). The count is unchanged by this patch, and `livestream` is fully green.

The new module avoids that environmental class entirely by supplying throwaway `LIVEKIT_*` settings and decoding the resulting JWT with the same throwaway secret — the token builder runs for real, so an identity or grant regression still fails the test.

## Baseline key set

A join that succeeds returns exactly: `livekit_url`, `token`, `room`, `role`, `duration_minutes`, `room_started_at`, `remaining_ms`, `is_host`, `features`, `limits`, `entitlement`, `cap_ends_at`. Adding a key is additive and allowed; removing or renaming one breaks three web apps and the Flutter app.

## Shared-copy drift

`node shared/sync.mjs --check` from the workspace root, read-only, no write:

```
✗ shiksha-teacher-dashboard/src/contexts/AuthContext.jsx
✗ shiksha-student-dashboard/src/contexts/AuthContext.jsx
✗ shiksha-teacher-dashboard/src/shared/ProfileSwitcher.jsx
✗ shiksha-student-dashboard/src/shared/ProfileSwitcher.jsx
✗ shiksha-teacher-dashboard/src/shared/SettingsModal.jsx
✗ shiksha-student-dashboard/src/shared/SettingsModal.jsx
6 file(s) drifted.
```

Exactly the six drifts the architecture-stage validation already recorded. Unchanged and untouched; `sync` was **not** run. RC-010 owns the reconciliation.

## Discovered mismatches

0. **The instant-meeting admit default already moved, and the architecture pack predates it.** The spec and the first draft of this suite were written against a working branch 17 commits behind `origin/main`, where `instant_create` set `admit_mode="open"` and any authenticated holder of the link joined directly. On current `origin/main` and `origin/dev` it sets **`admit_mode="lobby"`** — changed by `f572151`, "Allow approved anonymous guests in instant collaborations". A non-host now knocks and the host admits before any token is issued. Seven of the eight original tests failed against the real remote tips for this reason alone; the suite now sets `admit_mode` explicitly everywhere and pins the default in one dedicated test.

   This is load-bearing for the remote work rather than incidental. **A general meeting already has a host-admission consent step**, so the companion design builds on an existing gate instead of introducing the room's first one. `architecture-decisions.md` ADR-01 already requires the authorization adapter to preserve lobby gates; that requirement is now backed by a test. The 12-key join payload is **unchanged** across both bases — verified by extracting the view's success block from each — so no client contract broke, only the path to it.

1. **Container runtime cannot run the pinned Django.** `Dockerfile:1` is `FROM python:3.11-slim`; `requirements.txt` pins `Django==6.0.9`, which requires Python ≥3.12. Confirmed by reading both files, not inferred. The local venv is 3.12.3 so development and this test run are unaffected. **RC-004 owns this.** Note the workspace memory records production deploying via `scripts/deploy.sh` rather than this image, so the blast radius needs confirming before RC-004 assumes production is broken.
2. **A TEACHER-role account joining someone else's instant room gets the `STUDENT` in-room badge.** `role` is `"teacher"` only for an explicitly `invited_teacher` on a scheduled session. Correct per `join_group_session`, surprising on first read, and now pinned by `test_two_unrelated_teachers_can_meet` so RC-002 does not mistake the badge for an authorization input.
3. **`settings_test` runs Celery eagerly**, so `_schedule_hard_duration_cutoff`'s one-hour-out `apply_async(eta=…)` executes inline. The task's own timing guard correctly refuses to end a just-opened room, then raises `Retry`, logging a traceback per call. Harmless but noisy. The baseline stubs `hard_expire_group_session.apply_async` at the call site; the task's behaviour belongs to its own tests.

## Not claimed

No PostgreSQL locking proof (SQLite was used, which RC-001 permits for functional baseline only), no production behaviour, no LiveKit cloud connection, no browser test, no remote OS control, no native work of any kind. Next task: RC-003, which now carries explicit kill criteria.
