# RC-005 evidence — collaboration schema and constraints

Date: 11 November 2026. **First production-code change of this pack.** Everything is additive and unreachable: no view, route or serializer references the new tables, and `sessions_app/remote_control_views.py` is untouched.

## Delivered

| Artifact | What it is |
|---|---|
| `sessions_app/models.py` | `RemoteControlGrant` extended with the v1 fields; new `PairedDevice`, `FileTransfer`, `CollaborationAuditEvent` |
| `sessions_app/migrations/0019_remotecontrolgrant_approval_expires_at_and_more.py` | Additive migration — **zero** `RemoveField` / `DeleteModel` / `RenameField`, one `AlterField` |
| `sessions_app/tests_collaboration_models.py` | **26 tests**, all green |

`sessions_app` on this branch: **167 tests, 4 failures + 1 error** — the documented environmental LiveKit cases in `sessions_app.tests`, unchanged. `manage.py check`: no issues.

## The migration number was nearly wrong

Generated locally it came out as `0018`, which **already exists upstream** as `0018_instantcollaborationguest` (from `f572151`). The working branch is 17 commits behind `origin/main`, so `makemigrations` could not see the real leaf. Two same-numbered migrations in one app is the collision this repo has hit before.

Regenerated against the actual branch base; it is now `0019` and depends on `('sessions_app', '0018_instantcollaborationguest')`. `migrate --plan` shows 0018 then 0019. **Never run `makemigrations` from a branch that is behind the deploy target** — the number is derived from what the working tree can see, not from what exists.

The same staleness nearly caused a worse error: copying the local `models.py` onto the branch would have **deleted `InstantCollaborationGuest`**, because the local file predates it. The edits were applied to the branch's own file instead, and the guest model is asserted still present.

## Deliberate divergence from the specification — one, flagged

`database-specification.md` says the grant's `session` FK should be **PROTECT**. It is left as **CASCADE**.

`GroupSession` is registered in the Django admin, so admin deletion is a reachable path; switching to PROTECT would make deleting any session that ever had a grant raise `ProtectedError` — a behaviour change to the existing product, arriving as a side effect of an additive migration. The task's own non-negotiables require preserving existing behaviour.

The three **new** session FKs (`private_session`, `live_session`, `skill_session`) and `device` do use PROTECT as specified, because nothing depends on them yet and there is no existing behaviour to preserve.

**This needs the contract owner's decision**, not an implementer's: either accept CASCADE for the legacy group FK, or schedule the PROTECT change with a plan for admin deletion. Recorded rather than silently adopted.

## A real bug the constraint caught

The first draft left `session` as `NOT NULL` while the exactly-one-session check required it to be NULL for a private/live/skill grant. Every non-group grant would have been unenforceable at insert — the two rules contradicted each other and the table would have accepted only group rows. Caught by generating the migration and reading it rather than trusting the model. `session` is now nullable; the check constraint guarantees a row still has exactly one.

## What the tests actually assert

They drive inserts to the point the **engine** refuses them, not a Python guard — the value of a constraint is that it holds when a future caller forgets the service helper.

- Exactly one session FK: none → refused, two → refused, and each of the four shapes individually valid.
- A v1 row without `device`, without either binding, or without `public_id` → refused.
- `controller == target` → refused (same-account control is denied in MVP).
- **One live grant per device across rooms** — a second `approved`/`active`/`paused` grant on the same device in a *different* room is refused; the slot frees on a terminal status; `requested` does **not** reserve it.
- One pending request per (controller, device) — the flood guard.
- `public_id` unique; `capabilities` defaults to `[]`, never something permissive.
- **The legacy path still works**: a row with none of the v1 fields inserts, keeps `schema_version=0`, and does not occupy the device slot. This is what lets the change ship with no backfill and no edit to the existing view.
- `FileTransfer`: self-send refused, `recipient` NOT NULL (so it can never mean "everyone"), `stream_id` unique.
- `CollaborationAuditEvent`: the same (resource, version, event) cannot be logged twice — the audit write is idempotent under a retried transaction — and **deleting the actor keeps the row** (`SET_NULL`), so account deletion cannot erase the record that something happened.

## Not covered

**The concurrent two-room approval race is not tested.** `config.settings_test` is SQLite, which enforces CHECK constraints and partial unique indexes but does not reproduce PostgreSQL row locking. The tests prove the *constraint* refuses the second row; they do not prove the `select_for_update` ordering that is meant to stop two transactions reaching that point. RC-005's acceptance asks for this on real Postgres — now known to be **16.15** on both environments — and it remains open.

Also not done: forward/backward compatibility drill on an isolated Postgres instance, and a measured lock plan for the index creation. The table is small today, but "small" is an assumption nobody has measured on prod.

## Next

RC-006 (participant bindings and the CSRF boundary) is unblocked by this schema. It is the first task that adds a **reachable** route, so it is where the feature flag starts mattering.
