# RC-008 part 2a — the consent state machine

Date: 20 November 2026. Branch: `rc008-part2`, cut from
`docs/shiksha-remote-plan-on-dev`.

**RC-008 part 2 is split again, and this is 2a.** Part 1 built the
PostgreSQL 16 harness; its closing note listed what part 2 owed: the nine
states, atomic device reservation, the Ed25519 activation ticket, 1s leases
with a 3s deadline, the global kill flag, and demoting
`remote_control_views.py`. That is still ~1,500 lines, past the workspace's
~800 rule, so this pass takes the half the harness exists to prove — the
state machine — and leaves the crypto to 2b. Signing tickets on top of an
unproven machine would be the wrong order.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/grants.py` | the machine (**new**) |
| `sessions_app/tests_collaboration_grants.py` | **51 tests** (**new**) |
| `sessions_app/tests_collaboration_races.py` | +1 race, now **8** |
| `global_settings/models.py` + migration `0022` | `collaboration_control_enabled` |
| `sessions_app/collaboration/views.py` | binding-loss cascade filled in |
| `sessions_app/collaboration/devices.py` | unlink now goes through the machine |

    DJANGO_SETTINGS_MODULE=config.settings_test_pg venv/bin/python \
      manage.py test --noinput sessions_app.tests_collaboration_races
    → 8 tests, OK, on PostgreSQL 16.15

    DJANGO_SETTINGS_MODULE=config.settings_test venv/bin/python \
      manage.py test --noinput sessions_app.tests_collaboration_grants
    → 51 tests, OK

## The claim part 1 was built to prove

`ConsentStateMachineRaceTest` drives **the shipped code**, not a re-statement
of it. Two pending requests against one device, two threads, both calling
`grants.decide(allow=True)` at a barrier: exactly one becomes `approved`,
exactly one row holds the reservation, and the loser is refused `busy`.

🔴 **The loser's error code is asserted, not just the count.** A refusal of
`version_conflict` would also produce "one winner" while meaning something
quite different — that the two transactions interleaved on the *grant* row
instead of serialising on the *device* row. The count alone cannot tell those
apart, and the device lock is the thing under test.

RC-005's `DeviceReservationRaceTest` hand-rolls lock-then-reserve because
there was no state machine to call when it was written. Both are kept: drop
the device lock from `_lock_grant` and the hand-rolled test still passes
while this one fails, which is exactly the signal wanted.

## 🔴 A real defect, found by asserting a post-condition

The first implementation applied lazy expiry *inside* the same transaction as
the work. Every caller that finds an elapsed grant then **refuses** — 410
expired, 409 busy — and a refusal rolls the transaction back, **taking the
expiry write with it**.

The consequence is not cosmetic. `rc_grant_one_live_grant_per_device` is a
partial unique index with no `NOW()` in it, deliberately: a deadline that has
passed is not a state until some transaction writes it down. So a stale row
keeps holding the device slot, and a device can become permanently
un-requestable **while every single caller correctly reports it as expired**.
Only a *successful* writer would ever clear it.

Found by `test_an_elapsed_request_cannot_be_approved`, which asserted the row
had actually transitioned rather than stopping at "the right exception was
raised". The test passed its `assertRaises` and failed its post-condition.
A test written only against the exception would have shipped this.

Fixed with `_sweeps_expiry`: the sweep runs in its own committed transaction
*before* the decorated one opens. **Consequence for callers, and it is a
real constraint:** these functions must not be invoked inside an outer
`atomic` block, or the sweep rejoins that transaction and the defect returns.
`terminate_for_binding` and `terminate_for_device` are written accordingly —
each calls `terminate()` bare, not wrapped.

🔴 One caller still violates this: `devices.revoke_device` is `@transaction
.atomic` and calls `terminate_for_device` inside it. That is tolerable only
because `revoke_device` commits — if it ever grows a path that rolls back
after terminating, the expiry writes go with it. Left as-is rather than
restructured, and stated here rather than hidden.

## A second ordering bug the fix exposed

Once the sweep committed the expiry, `decide` started answering **409
version conflict** instead of **410 expired** — the sweep had bumped the
version the caller was holding. 409 invites a refetch-and-retry loop against
a grant that can never be acted on again. `_assert_live` now reports the
terminal state before the version check, so the terminal, actionable answer
wins. Tests assert the status code, not just that something was refused.

## Decisions worth knowing before touching this code

**A second kill flag, not a reused one.** `collaboration_control_enabled`
(default **False**) is additive and narrower than
`collaboration_bindings_enabled`. Killing control by turning bindings off
would blank the room UI for everyone in a live session; an operator stopping
a control incident wants the room to keep working. Both must be on to
request, approve, activate or renew.

🔴 **Stopping deliberately ignores the kill flag.** `terminate` does not call
`assert_control_enabled()`. If it did, flipping the switch would *strand*
live grants instead of ending them — the precise opposite of what the switch
is for. Tested.

**A browser approval mints nothing.** `decide(allow=True)` lands in
`approved` with no epoch and no `expires_at`. Only `activate`, which needs
device authentication plus a one-use consent nonce plus `local_confirm is
True`, can reach `active`. The protocol says it twice and this is the single
most tempting shortcut in the design, so it is tested from both directions.

**OD-01 is restated, never inferred.** `group_session_policy
.remote_control_enabled` is `True` for scheduled rooms and `False` for
instant ones — the **reverse** of OD-01's rule for this plane (peer control
inside instant collaborations, teacher-initiated only in scheduled ones).
It is a legacy attribute for the dormant path. A regression that starts
reading it would invert two tests, which is why both directions are asserted.

**Requesting does not reserve.** Reservation happens on approval. Otherwise
a controller could park a request on a device and lock everyone else out
without the owner ever agreeing. What a request does hold is the
one-pending-per-(controller, device) slot, against flooding.

**Resume never extends the hard expiry**, and every activation — resume
included — mints a **fresh epoch**, so no packet from a previous epoch can be
replayed into the new one.

## What this does NOT do

* **No activation ticket, no leases.** 2b owns the Ed25519
  `shiksha-control+jwt`, the `shiksha-control-lease+jwt`, lease_seq
  monotonicity and nonce freshness. `activate` performs the transition and
  returns the row; nothing yet mints authority from it. This is why the
  machine is provable without a signing key in the test environment.
* **`remote_control_views.py` is untouched.** Its demotion to a compatibility
  rejecter is 2b. It still writes legacy (`schema_version=0`) rows, which the
  new machine refuses to address at all — `_lock_grant` 404s them, tested.
* **Host bindings do not exist.** RC-009 owns them; `snapshot` still returns
  `host_bindings: []` and `ws_ticket` still 422s one. `host_binding_id` is
  stored and carried but **its liveness is not verified**, because there is
  nothing to verify it against yet. RC-009 must add that check — a grant
  whose host binding has lapsed is currently only caught when the binding
  deletion cascade fires, not on activation.
* **No route is wired.** Nothing in `urls.py` reaches this module; the only
  live call sites are the two cascades. The feature remains absent behind
  both flags.

## Inherited constraints, carried forward to 2b

* From RC-006b: **re-check authorization when an event is published**, never
  infer it from channel-group membership.
* From the contract: **the 1-second lease must be a DB read plus Redis, never
  a row write** — the box dies at ~20 concurrent controls otherwise.
* From part 1: **the companion must serialise refreshes behind a
  single-flight lock**, or a double-submitted refresh unlinks the device.

## Next

**RC-008 part 2b** — signing keys and ticket/lease minting, the legacy
demotion, and the routes from `api-contracts.md` §Control grants. Then
RC-009, which is what finally makes `host_binding_id` mean something.
