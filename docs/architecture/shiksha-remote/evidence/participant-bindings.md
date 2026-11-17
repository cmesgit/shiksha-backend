# RC-006a evidence — participant bindings and the CSRF/Origin boundary

Date: 17 November 2026. Branch: `docs/shiksha-remote-plan-on-dev`.

**RC-006 was split; this is the HTTP half.** The task lands ~1600 lines, and
the workspace rule is to split past ~800. The split point is not arbitrary:
dev runs gunicorn on :8001 and therefore **has no WebSockets at all**, so the
WS half cannot be validated on dev until a uvicorn staging exists. Everything
here can be. RC-006b is `ws-ticket/` plus the `/ws/collaboration/v1/`
consumer and its routing; `mint_ticket`/`consume_ticket` already ship here,
tested, so that half has nothing left to invent.

**These are the first *reachable* collaboration routes** — RC-005 was
additive and unreachable. They are also the first routes in this repo that
validate CSRF at all.

## Delivered

| File | What |
|---|---|
| `global_settings/models.py` + migration `0021` | `collaboration_bindings_enabled` (default **False**), and a rewrite of `live_remote_access_enabled`'s help text |
| `sessions_app/collaboration/policy.py` | eligibility, principal resolution, session adapters, origin allowlist |
| `sessions_app/collaboration/bindings.py` | the expiring Redis store, LiveKit SID verification, single-use tickets |
| `sessions_app/collaboration/auth.py` | CSRF, exact-Origin, Idempotency-Key, the error envelope, the flag gate |
| `sessions_app/collaboration/views.py` | the five HTTP routes |
| `sessions_app/urls.py` | mounts them under `/api/sessions/collaboration/v1/` |
| `sessions_app/tests_collaboration_policy.py` | **52 tests** |

Routes, all exactly as `api-contracts.md` specifies them:

    GET    collaboration/v1/csrf/                      {csrf_token}
    POST   collaboration/v1/bindings/                  201 {binding_id, identity,
                                                           participant_sid,
                                                           expires_in: 6, capabilities}
    POST   collaboration/v1/bindings/<id>/heartbeat/   {expires_in: 6, server_time}
    DELETE collaboration/v1/bindings/<id>/             204
    GET    collaboration/v1/snapshot/?binding_id=      {server_time, host_bindings,
                                                        grants, transfers}

`views.py` is not in the task's "Files to create" list, which names only
policy/bindings/auth. It is a necessary sibling rather than scope creep —
the alternative was putting five DRF handlers inside `auth.py`. `urls.py`
entries were inlined into the existing `sessions_app/urls.py`, which *is* in
the modify list, rather than adding a sixth module.

## The flag, and the flag that is not the flag

`collaboration_bindings_enabled` ships **False** and every route answers
**404** while it is off — absent, not forbidden, so the surface is not even
probeable before the release gates.

🔴 Migration `0021` also rewrites `live_remote_access_enabled`'s help text, as
REMOTE.md §7 trap 7 requires. It defaulted `True` and read *"Master switch for
teacher screen-control of a student"* — an admin reading that would
reasonably conclude this feature was already on. It now says it gates the
dormant legacy path in `remote_control_views.py`, that it grants nothing
(nothing sets `is_sharing_screen`, so every legacy request 409s), and that
the new switch is the other field.
`FeatureFlagTest.test_live_remote_access_enabled_does_not_turn_this_on`
pins that the legacy flag cannot enable this feature.

## Fail-closed, inverting the house style

🔴 `accounts/revocation.py` and `chat/redis_utils.py` both document
themselves as failing **OPEN** — "Cache-miss behaviour is deliberately
FAIL-OPEN", "EVERY function here fails OPEN and never raises" — so a Redis
outage costs availability rather than access.

`collaboration/bindings.py` fails **CLOSED**: a binding that cannot be read
is a binding that cannot be trusted, and the caller gets 503. This is
commented loudly in the module, because a reviewer tidying it to match its
neighbours would convert a Redis blip into unauthenticated remote control.

The idempotency cache in `auth.py` deliberately keeps the **open** behaviour,
and the asymmetry is the point: losing a replay record costs at-most-once
execution, not authorization. Failing closed there would turn a Redis blip
into a permanent 409 on a key the client cannot change.

Equally deliberate: **`RosterUnavailable` is 503, never 403.** "We could not
ask LiveKit whether you are in the room" and "your SID is wrong" are
different answers, and only one is worth a retry. A test pins it.

## The capacity rule is a test, not a comment

🔴 The box's wall is PG `max_connections` (~75) and ASGI leaks roughly one
connection per request. A 2s-per-participant heartbeat that wrote a row would
exhaust it at around 20 concurrent sessions.

`HeartbeatTest.test_a_heartbeat_writes_nothing_to_the_database` runs the
heartbeat inside `CaptureQueriesContext` and asserts **zero**
INSERT/UPDATE/DELETE. `test_a_heartbeat_does_not_query_livekit_per_call`
asserts the heartbeat path reaches LiveKit **zero** times. The roster is
coalesced to one query per *room* per 2s, pinned by
`test_the_room_roster_is_coalesced_not_queried_per_request`.

One refinement the tests forced: that 2s coalescing window would refuse
anyone who joined mid-window on their **first** bind, and "just retry" is not
part of this endpoint's contract. `verify_participant_sid(...,
allow_refresh=True)` re-asks LiveKit once, and only when the identity is
*absent* (not when the SID merely mismatches), only on binding creation —
once per tab — and never on the heartbeat path.

## Auth sid is mandatory

The contract: *"Missing legacy auth sid means collaboration requires a normal
login refresh/re-auth that supplies a tracked session; it never degrades to
account-only authority."* `principal_from_request` refuses a token with no
`sid` claim rather than falling back to the account, and a binding is pinned
to `(user, auth_sid, context, profile_id)` — re-logging in or switching
profile does not inherit its standing. Three tests cover those.

## Tests

    cd ~/workspace/projects/worktrees/remote-be
    export TEST_DB_PATH=/tmp/claude-1000/rc006.sqlite3 \
           DJANGO_SETTINGS_MODULE=config.settings_test
    .venv/bin/python manage.py test sessions_app.tests_collaboration_policy
    .venv/bin/python manage.py test sessions_app livestream global_settings
    .venv/bin/python manage.py makemigrations --check --dry-run

| Suite | Result |
|---|---|
| `sessions_app.tests_collaboration_policy` | **52 tests, OK** |
| `config` | **29 tests, OK** |
| `sessions_app livestream global_settings` | **388 tests, 4 failures + 1 error** |
| `makemigrations --check --dry-run` | **No changes detected** |
| `migrate global_settings --plan` | `0021…` applies after `0020_merge_20261115_2144` |

🔴 **The five non-passes are the documented environmental LiveKit cases,
unchanged by name** since the 17 November baseline:
`test_request_creates_participant`, `test_request_with_group_students`,
`test_student_can_request_session` (all `RequestSessionTest`),
`test_participant_can_view` (`SessionDetailTest`), `test_start_session`
(`SessionLifecycleTest`) — all in `sessions_app.tests`. Do not fix them and
do not let the count move.

Migration numbering was checked before generating, per the rule that nearly
shipped an `0018` collision in RC-005: `global_settings` tops out at
`0020_merge_20261115_2144` on **both** the branch and `origin/dev`, and both
plan branches were **0 behind origin** at the time. The new migration is
`0021`, not a second `0020`.

## Two things the tests found that review would not have

**1. Django's CSRF middleware has its own Origin check.** The first run of
this suite failed every unsafe call with `csrf_failed`. The real reason,
recovered by instrumenting `_CSRFCheck._reject`, was
`Origin checking failed - https://app.test.shikshacom.com does not match any
trusted origins` — `CsrfViewMiddleware` validates `Origin` against
`CSRF_TRUSTED_ORIGINS` *before* comparing tokens.

So **the collaboration allowlist must be a subset of
`CSRF_TRUSTED_ORIGINS`**, or every unsafe call 403s with a message pointing
nowhere near the cause. Production already satisfies this
(`CORS_ALLOWED_ORIGINS ⊆ CSRF_TRUSTED_ORIGINS` in both env files) but nothing
enforced it. `test_the_real_environments_satisfy_that_coupling` now parses
`settings_dev.py` and `settings_prod.py` with `ast` and asserts it — read
statically because `settings_prod` raises `ImproperlyConfigured` without a
real `SECRET_KEY`, and that guard should not be worked around to run a test.

**2. A mutation found a real hole in the origin tests.** See below.

## Mutation sweep — 14 mutations, and one of them survived

Every negative assertion was re-run against deliberately broken code
(`/tmp/claude-1000/mutate_rc006.sh`). 13 of 14 were caught on the first pass.

| # | Mutation | Caught? |
|---|---|---|
| M1 | CSRF check becomes a no-op | ✅ 2 failures |
| M2 | Origin check becomes a no-op | ✅ 3 failures |
| M3 | exact match → `endswith` | 🔴 **SURVIVED** — see below |
| M4 | missing auth sid falls back to the account | ✅ |
| M5 | feature flag ignored | ✅ 2 failures |
| M6 | idempotency guard disabled | ✅ 4 failures |
| M7 | binding store fails OPEN | ✅ |
| M8 | unknown-field rejection removed | ✅ |
| M9 | heartbeat writes a row | ✅ |
| M10 | roster coalescing removed | ✅ |
| M11 | ownership ignores auth_sid/context/profile | ✅ 2 failures |
| M12 | LiveKit outage collapses into 403 | ✅ |
| M13 | stale-presence check removed | ✅ 2 classes |
| M14 | ticket is replayable | ✅ |

🔴 **M3 is the finding.** The suite had one "evil origin",
`https://app.test.shikshacom.com.attacker.net` — which **ends in
`.attacker.net`**, so a naive `origin.endswith("app.test.shikshacom.com")`
already rejects it. It tests `startswith`/substring bugs, not the classic
allowlist bug.

The attack that actually defeats `endswith` ends **with** the allowed host:
`https://evilapp.test.shikshacom.com`. An attacker only needs a name that
looks like a subdomain. Until that case existed, swapping the exact
comparison for `endswith` left the entire origin suite green.

Both lookalikes are now tested, plus a scheme downgrade
(`http://` against an `https://` allowlist). M3 re-run with the new tests:
**FAILED (failures=1)** — `test_a_lookalike_suffix_origin_is_refused`, which
is the correct test failing for the correct reason. The suite is **52 tests**
with the hole closed, up from 50.

This is the second time in two tasks that mutation testing caught a test
that proved less than it claimed (RC-031's was Django's `MultiPartParser`
already sanitising filenames). **Treat "the suite is green" as unverified
until the suite has been shown to go red.**

## Not done here

1. **RC-006b** — `ws-ticket/` route, the `/ws/collaboration/v1/` consumer,
   `sessions_app/routing.py` and `config/asgi.py` wiring. 🔴 Note for whoever
   takes it: `config/asgi.py` has **no `OriginValidator` and no
   `AllowedHostsOriginValidator`** on the websocket stack today, and cookies
   are `SameSite=None; Domain=.shikshacom.com` — so any origin can already
   open an authenticated socket to the *existing* consumers. The new consumer
   must do its own exact-Origin check; wrapping the shared middleware would
   change behaviour for every existing socket and is out of scope.
2. **Rate limits are not implemented.** The contract specifies per-route
   limits (30/min/user on `csrf/`, 10/min/user on `bindings/`, 30/min/binding
   on `snapshot/`). DRF throttling is configured in this project and the
   scopes would be additive; left out to keep this slice reviewable. **They
   are a release-gate item, not optional** — recorded here so the gap is not
   discovered at rollout.
3. **`snapshot/`'s `host_bindings` is always `[]`.** Not a simplification:
   no host binding can exist until RC-007/RC-009 build the model and routes.
   The key is present and correctly shaped so the client contract does not
   change when they land.
4. **Guest (`InstantCollaborationGuest`) bindings are not reachable.** OD-01
   puts un-authenticated lobby guests in scope, but every row in the contract
   marks `bindings/` as `B` = authenticated browser cookie, and a guest holds
   a signed credential instead. That path needs its own principal resolver
   and is not specified in RC-006. **Flagged as a contract gap**, not an
   oversight — RC-011 cannot ship the OD-01 peer-to-peer story without it.
5. **No PG-16 test.** Nothing here takes a row lock, so there is no race to
   test yet; RC-008's harness is still the place that has to exist.

## Rollback

`git revert` of the commit, then `migrate global_settings 0020`. The
migration adds one nullable-by-default boolean and changes one help string;
no data is written or read by anything while the flag is off.

## Next integration task

**RC-006b** (the WS half), then **RC-007** (DPoP pairing — mandatory crypto
review). RC-003's hardware spike remains the long pole and is unblocked by
none of this.
