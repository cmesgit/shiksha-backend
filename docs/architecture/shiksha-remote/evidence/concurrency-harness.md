# RC-008 part 1 — the PostgreSQL 16 concurrency harness

Date: 17 November 2026. Branch: `docs/shiksha-remote-plan-on-dev`,
cherry-picked to `-on-main`.

**RC-008 is split. This is part 1.** The consent state machine, the signed
activation ticket, the bounded leases and the legacy demotion are part 2 —
easily 1,500 lines on their own, past the workspace's ~800 rule. The harness
comes first because it is the thing *three previous tasks already owed*, and
because part 2's core claim ("simultaneous grants, one winner") cannot be
proven without it.

## The finding that made this possible

🔴 **This box has PostgreSQL 16.15 installed and running, with peer auth for
the local user.** A 95-day-old note in my memory said there was no local
Postgres, and three evidence files were written around that assumption:

    RC-005  "needs real PG 16"
    RC-031  "unprovable locally … add it to the PG-16 harness RC-008 has to build"
    RC-007b "select_for_update is a silent no-op on SQLite"

It took one `which psql` to overturn. **Re-check an environment assumption
before building a plan around it** — the deployed target is 16.15 and this
box runs 16.15, so the lock semantics exercised here are the real ones, not
an approximation.

## Delivered

| File | What |
|---|---|
| `config/settings_test_pg.py` | PostgreSQL test settings (**new**) |
| `sessions_app/tests_collaboration_races.py` | **7 tests** (**new**) |

    createdb shiksha_rc_pg
    DJANGO_SETTINGS_MODULE=config.settings_test_pg \
      .venv/bin/python manage.py test --noinput sessions_app.tests_collaboration_races

    → 7 tests, OK, on PostgreSQL 16.15
    → the same module under settings_test: 4 skipped, loudly

## The three debts, paid

| Owed by | Claim now proven |
|---|---|
| **RC-005** | Two rooms approving the same device concurrently produce exactly one winner — and exactly one row |
| **RC-031** | Five concurrent uploads against a quota of three admit exactly three |
| **RC-007b** | Two simultaneous refreshes produce exactly one rotation |

Plus two tests that check the harness itself before it checks anything else:
the backend really is PostgreSQL 16, and `select_for_update` really blocks
here — proven by timing, since a second thread that acquires a held lock
without waiting is the SQLite behaviour this module exists to exclude.

## 🔴 A product consequence the race exposed

**Two simultaneous refreshes do not merely lose a rotation — they unlink the
device.**

The loser presents a token the winner has just moved into
`previous_credential_hash`, which is *indistinguishable* from a stolen token
being replayed. RC-007b's reuse detection fires and revokes the device. That
is the designed response and it is correct — but it means **a companion that
double-submits a refresh (a retry, two threads, a flaky network) unlinks
itself and the user must pair again.**

This is a real constraint on RC-012, not a curiosity: **the companion must
serialise refreshes behind a single-flight lock.** It should be stated in
the companion's implementation notes before anyone writes the refresh loop,
because the failure is silent at the client and destructive at the server.

## 🔴 A pre-existing repo defect, found and NOT fixed

The full migration chain **does not apply on PostgreSQL**:

    django.db.utils.ProgrammingError:
      column "explanation" of relation "quizzes_question" already exists

A migration in the unrelated `quizzes` app is idempotent on SQLite and not
on PostgreSQL. Since every environment is already migrated, nothing is
broken today — but **a from-scratch PostgreSQL build of this database fails**,
which matters for CI, for a new environment, and for disaster recovery.

It is out of RC-008's scope and faking it silently would be worse, so
`settings_test_pg` builds the schema from the current **model state**
instead of replaying migrations, and says so in its docstring. The
consequence is stated rather than hidden: **this harness proves runtime lock
semantics, not migration correctness on PostgreSQL.** The latter deserves
its own task.

## Two mistakes worth recording

1. 🔴 **The first draft deadlocked itself.** The barrier sat *inside* the
   lock-holding transaction, so the second thread blocked on the row lock,
   never reached the barrier, and both threads died with
   `BrokenBarrierError`. That failure was the lock working correctly and the
   test being wrong. **A barrier synchronises the START of contention; it
   can never be held across the contended resource.**
2. 🔴 **The refresh-race test originally asserted the wrong ending** — that
   the winner's credential survives. It does not: reuse detection revokes
   the device and nulls the hash. Writing down the expected outcome before
   running it is what surfaced the product consequence above; a test written
   to match whatever the code did would have recorded nothing.

## Capacity note

`CONN_MAX_AGE = 0` in `settings_test_pg` is load bearing. A persistent
connection shared across the harness's threads would serialise them, and the
race would never occur — leaving a harness that passes by never running its
own scenario. Every worker closes its connection explicitly for the same
reason the production code is careful here: `max_connections` is this
platform's capacity wall.

## Next

**RC-008 part 2** — the consent state machine itself: the nine states from
`remote-control-protocol.md`, atomic device reservation (now provable),
the Ed25519 `shiksha-control+jwt` activation ticket, 1s leases with a 3s
deadline and nonce freshness, the global kill flag, and demoting
`remote_control_views.py` to a compatibility rejecter that can never mint
native authority.

Two constraints it inherits, both already written down:

* 🔴 From RC-006b: **re-check authorization when an event is published**,
  never infer it from channel-group membership.
* 🔴 From the contract: **the 1-second lease must be a DB read plus Redis,
  never a row write** — the box dies at ~20 concurrent controls otherwise.
