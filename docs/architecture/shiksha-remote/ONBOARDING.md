<!-- ───────────────────────────────────────────────────────────────
🔴 SYNCED COPY. The working original lives OUTSIDE this repo, at
   ~/workspace/projects/work/REMOTE_ONBOARDING.md on the owner's laptop, in a
   directory that is NOT under version control.

   That is why this copy exists: without it, no other machine can read
   this file at all.

   CONSEQUENCE: there are now TWO copies and they WILL diverge. If you
   edit one, copy it to the other in the same change. If they disagree,
   check `git log -1 --format=%cd -- docs/architecture/shiksha-remote/ONBOARDING.md` against the laptop file's
   mtime and trust the newer one.
──────────────────────────────────────────────────────────────── -->

# Shiksha Remote ("Astra") — working on this from another machine or chat

**Purpose of this file:** get a second computer, or a second Claude chat,
productively onto this work **without redoing or destroying what already
exists**. It is deliberately short on design (that is `REMOTE.md`) and long
on coordination, because coordination is what has actually gone wrong here.

Written 17 Nov 2026. If the facts below disagree with the repo, **the repo
wins** — see §1.

---

## 1. Before you write a single line: verify, don't trust

Every status claim in this repo has been stale at least once, including in
`CLAUDE.md`, including in `REMOTE.md`, including in the memory index. The
branch is the only thing that cannot lie.

```bash
cd ~/workspace/projects/worktrees/remote-be      # or your checkout
git fetch origin
git log --oneline -8                             # what is actually done
git status --short                               # what is half-done RIGHT NOW
git rev-list --count @{upstream}..HEAD           # unpushed commits
```

Read those four outputs before you believe any document, including this one.

🔴 **`git status` is not optional.** Uncommitted work in a worktree is
invisible to every other machine. The whole reason this file exists is that
1,391 lines of RC-008 part 2 were sitting uncommitted on one laptop while
`REMOTE.md` cheerfully told every other reader that RC-008 part 2 was the
next thing to start.

---

## 2. 🔴 The multi-machine / multi-chat protocol

More than one chat and more than one computer touch this branch. There is **no
PR and no review gate** to catch a collision, so the discipline has to be
manual.

**Before starting a task**

1. `git fetch && git log --oneline -8` — is it already done?
2. `git status --short` — is someone mid-way through it *right now*?
3. Check §3 of `REMOTE.md` (the done table) **and** §6 (next actions).
4. If `git status` is dirty with files related to your task, **stop and ask
   the human.** Do not commit work you did not write; you cannot tell a
   deliberate half-state from a broken one.

**While working**

- One task per commit. The commit message is the handoff — this project's
  messages are long on purpose.
- Never `git checkout` another branch in a worktree that has uncommitted
  work in it. Use a second worktree instead.

**Before you stop**

- **Push.** An unpushed commit is the same problem as an uncommitted file.
- Update `REMOTE.md` §3 (done table), §6 (next actions) and add a numbered
  section for what your task decided and left open.
- Update the one-paragraph Astra block in `CLAUDE.md` — it is the first
  thing the next chat reads, and it has been wrong twice.

**Known live state:** do not trust a status paragraph in this file — it has
been wrong before. `HANDOFF.md` §3 and §5 are the maintained status; read
those, then verify against `git log` yourself (§1 above).

---

## 3. Getting the code

🔴 **The spec pack and all the code are on two branches, not on `main`.**
`git ls-tree origin/dev docs/architecture/` returns nothing. A fresh clone
that checks out `main` or `dev` will not see the 57-doc pack, the contracts,
or any of the collaboration code, and will reasonably conclude none of it
exists.

| Branch | Base |
| --- | --- |
| `docs/shiksha-remote-plan-on-dev` | `origin/dev` |
| `docs/shiksha-remote-plan-on-main` | `origin/main` |

They are **meant to carry byte-identical application code**, differing only
in base — the same commit cherry-picked onto each. Verify with
`git diff --stat <tipA> <tipB> -- sessions_app/ docs/` (expect empty) **before
trusting that claim**: as of 26 Nov 2026 it does not hold — `-on-main` is
missing RC-008 part 2, RC-009 and the RC-003 evidence files that `-on-dev`
already has (~8,260 lines). Cherry-pick the gap across before starting new
work on `-on-main`, or confirm with the owner that it's deliberately deferred.

**There is no PR, deliberately — the owner's standing call.** Do not open one
without asking.

```bash
git clone git@github-work:cmesgit/shiksha-backend.git
cd shiksha-backend
git checkout docs/shiksha-remote-plan-on-dev
```

On the original laptop, work from the worktree rather than the primary
checkout — that checkout habitually carries another branch's uncommitted work:

```bash
cd ~/workspace/projects/worktrees/remote-be
```

---

## 4. Machine setup from scratch

Everything below is backend-only. Nothing in Astra is user-visible yet and
no frontend app is involved.

**Required:** Python **3.12** (3.11 will not install Django 6.0.9 — the
`Requires-Python: >=3.12` is enforced by pip at resolution).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 64 packages
```

🔴 **Do not copy the `.venv` symlink trick from `REMOTE.md`.** That line
(`ln -sfn .../shiksha-backend/.venv .venv`) only works on the original laptop
where a sibling checkout already has a venv. On a fresh machine, build a real
venv as above.

**Not required, and actively wrong to use:**

- **Docker.** Waived by the owner — it is not how Shiksha deploys, the image
  could not build, and `docker-compose.yml` pins `postgres:15` while both
  real environments run 16. It is fine as a *test harness* for PostgreSQL;
  it is not a deployment target.
- **LiveKit credentials.** Absent everywhere locally. See §5.
- **Rust / a Windows box.** Needed for RC-003 and the whole native track;
  present on no machine today. That is a procurement problem, not a setup
  step.

---

## 5. Running the tests

```bash
export DJANGO_SETTINGS_MODULE=config.settings_test
export TEST_DB_PATH=/tmp/rc.sqlite3

.venv/bin/python manage.py test sessions_app livestream global_settings
.venv/bin/python manage.py test config
.venv/bin/python manage.py makemigrations --check --dry-run   # expect "No changes detected"
```

Expected on a correct tree (17 Nov): **532 tests, 4 failures + 1 error.**

🔴 **Those 5 non-passes are environmental and permanent** — there are no
LiveKit credentials locally. Do not fix them. **The assertion that matters is
that the count does not move**: it went 264 → 388 → 447 → 498 → 532 as tasks
landed and the failure count never changed. They are, by name:
`test_request_creates_participant`, `test_request_with_group_students`,
`test_student_can_request_session` (`RequestSessionTest`),
`test_participant_can_view` (`SessionDetailTest`), `test_start_session`
(`SessionLifecycleTest`) — all in `sessions_app.tests`.

If you see a **sixth** failure, that one is yours.

**The contract suites are standalone** — `docs/` has no `__init__.py`, so
Django's test discovery cannot reach them:

```bash
cd docs/architecture/shiksha-remote/contracts
python -m unittest test_contracts test_openapi      # 13 + 14
```

**The PostgreSQL harness** (RC-008 part 1) needs a real PG 16, because SQLite
does not enforce row locking and **silently no-ops `select_for_update()`** —
every lock assertion passes vacuously on SQLite.

✅ **On hruaia's laptop, PostgreSQL 16.15 is already installed and running**
with peer auth for `hruaia` — same major *and* minor as dev/prod. A memory
claiming "no local Postgres" was 95 days stale and had caused RC-005, RC-031
and RC-007b to each defer their race proof to a harness nobody thought could
be built. One `which psql` overturned all three. **Re-check an environment
assumption before building a plan on it.**

```bash
createdb shiksha_rc_pg
export DJANGO_SETTINGS_MODULE=config.settings_test_pg
.venv/bin/python manage.py test sessions_app.tests_pg_concurrency --noinput   # 7 tests
```

On a machine without it, a Docker PostgreSQL **16** is fine — Docker is
waived as a *deployment* target, not as a test harness. Don't conflate the
two. The suite skips loudly rather than silently if it finds SQLite, and it
checks *itself* first (that the vendor really is PG 16, and that
`select_for_update` really blocks, by timing) — because otherwise every
assertion could pass on a backend that ignores locks.

🔴 **Do not expect `manage.py migrate` to work on a fresh PostgreSQL.** There
is a **pre-existing repo defect, found but NOT fixed**: the migration chain
does not apply from scratch on PG (`column "explanation" of relation
"quizzes_question" already exists` — a `quizzes` migration that is idempotent
on SQLite but not on PG). Nothing is broken today because every real
environment is already migrated, but **a from-scratch PG build fails**, which
matters for CI and disaster recovery. `settings_test_pg` sidesteps it by
building schema from **model state**, so the harness proves *runtime lock
semantics, not migration correctness on PG*. That gap needs its own task.

---

## 6. 🔴 Mutation-check every negative test

This is the single highest-value habit on this project, and it is not
optional folklore — it has caught a bad test in **every** task that used it:

- **RC-031:** four tests asserted that `../../etc/passwd` is reduced to
  `passwd`. They passed with the sanitiser **deleted** — Django's
  `MultiPartParser` already strips paths and NULs before a view sees a
  filename.
- **RC-006a:** the origin suite's "evil origin" ended in `.attacker.net`,
  which `endswith()` already rejects. Swapping the exact comparison for
  `endswith` left the whole suite green. The attack that actually defeats
  `endswith` ends **with** the allowed host (`evilapp.example.com`).

The method: break the implementation on purpose, re-run, confirm the right
tests go red, restore. Verify the tree afterwards with `git diff`.

🔴 **Run mutation sweeps with `run_in_background`, never under a foreground
timeout.** One was killed mid-sweep and left a live mutation on disk while a
grep "integrity check" reported the tree clean.

**Treat "the suite is green" as unverified until the suite has been shown to
go red.**

---

## 7. The rules that bite, in one list

Full reasoning is in `REMOTE.md` §7 and the numbered per-task sections. These
are the ones that cost real time when missed:

1. **Never run `makemigrations` from a branch behind its deploy target.** The
   number comes from what the tree can see; this nearly shipped a duplicate
   `0018`. Check `git rev-list --count HEAD..@{upstream}` first.
2. **Never copy a whole `models.py` from another tree onto the branch** — a
   stale copy would have deleted `InstantCollaborationGuest`.
3. **`collaboration/bindings.py` fails CLOSED.** Every other cache helper in
   this repo (`accounts/revocation.py`, `chat/redis_utils.py`) documents
   itself as failing OPEN. Do not "tidy" the collaboration one to match —
   that converts a Redis blip into unauthenticated remote control.
4. **The heartbeat must never write to the database.** PG `max_connections`
   (~75) is the capacity wall and ASGI leaks ~1 connection per request. There
   is a test asserting zero writes; if you make it fail, the box dies at ~20
   concurrent controls.
5. **`live_remote_access_enabled` defaults `True` and is NOT this feature's
   switch.** It gates the dormant legacy path. The real gate is
   `collaboration_bindings_enabled`, which defaults `False`.
6. **An origin allowlist must be a subset of `CSRF_TRUSTED_ORIGINS`** —
   Django's own CSRF middleware runs an Origin check first, and the rejection
   message points nowhere near the cause.
7. **`CollaborationAuditEvent` must never gain a session FK.** Its
   independence is what keeps the audit trail alive through a cascade delete.
8. **Flipping a feature flag needs a re-login** to show up in
   `Admin-dashboard` — it freezes `feature_flags` at login. Put this in any
   rollout step or an operator reports the flip as broken.

---

## 8. Where things stand

Superseded by `HANDOFF.md` §3 (done/next), §5 (what RC-012 needs) and §7
(release gates) — written 21 Nov 2026, after this file. Read that instead of
trusting a status list here; this section is intentionally not duplicated so
it can't drift out of sync with that one again.

---

## 9. Reading order for a brand-new chat

1. `CLAUDE.md` — the Astra paragraph (then distrust its status claims, §1).
2. **This file.**
3. `REMOTE.md` — §2 (git), §3 (state), §6 (next), §7 (traps), then the
   numbered per-task sections for whatever you are touching.
4. `docs/architecture/shiksha-remote/README.md` — the spec pack entry point
   and its deliverable→document map (on the branch, not on `main`).
5. `docs/architecture/shiksha-remote/tasks/phase-N/RC-0NN.md` — the specific
   task prompt.
6. `docs/architecture/shiksha-remote/evidence/` — **what was actually run and
   measured**, as opposed to designed. Eleven files. When the design and the
   evidence disagree, the evidence is the honest record.
