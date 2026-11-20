# RC-009b — a companion is not a person in the room

Date: 21 November 2026. Branch: `rc008-part2`.

RC-009a built host bindings and the companion's identity. This is the
integration half: keeping that identity out of human accounting, and making
`snapshot` tell the truth.

## Delivered

| File | What |
|---|---|
| `livestream/views.py` | companion guard on both webhook handlers |
| `sessions_app/collaboration/host_bindings.py` | per-room index |
| `sessions_app/collaboration/views.py` | `snapshot` lists real availability |
| `sessions_app/tests_companion_presence.py` | **8 tests** (**new**) |

    → 55 tests OK, including livestream.tests / tests_attendance_profiles /
      tests_end_reconciliation unchanged
    → 270 tests OK across the collaboration suites

## 🔴 The specific failure this prevents

`group_attendance.parse_user_id` is two lines:

```python
parts = identity.split("_", 1)
return parts[0] if parts else None
```

Group-session identities are `{user.id}_{session.id}`. The companion joins
**the same room**, so without a guard its `rcdev_<uuid>` identity reaches
that function and yields the literal string **`"rcdev"` as a user id**. The
exclusion therefore has to happen *before* the parse — which is why the
guard sits at the top of `_handle_participant_join` and
`_handle_participant_left`, before any session lookup, rather than inside
the group-session branch.

Placing it there also covers LiveSession, PrivateSession and SkillSession
rooms in one move, instead of leaving three more copies of the same bug to
be found later.

**Both handlers, symmetrically.** If join excluded the companion and leave
did not, a companion disconnect would run `close_intervals` for an interval
that was never opened, against whichever user the legacy parse invented.

## 🔴 Excluding on the prefix, not on registration

The task's wording is "exclude registered rcdev identities". The
implementation deliberately excludes on the **reserved prefix alone**,
because gating on registration leaves precisely the gap the guard exists to
close: an unregistered — forged, or merely stale — prefixed identity would
fall through to the legacy parse.

The prefix test is fail-safe. The worst a forged `rcdev_` identity achieves
is excluding *itself* from a count it was never entitled to appear in.

Trust runs the other way and never comes through here: granting a
participant any companion privilege still requires
`companion_tokens.registered_companion`, which demands a live host binding
for that room. An unregistered companion-shaped identity is logged, because
it is a genuine anomaly — quietly, because a noisy log driven by an
attacker-controlled field is its own problem.

`test_a_name_that_merely_starts_similarly_is_not_excluded` guards the other
direction: `rcdevil_…` must not match, or the guard would start excluding
real humans from attendance.

## Capacity needed no guard, and that is worth writing down

`group_session_views` gates instant capacity on
`GroupSession.active_connections`. That counter is incremented by the
**chat WebSocket consumer** (`sessions_app/consumers.py`), not by LiveKit
webhooks — and the companion never opens that socket. It opens the
collaboration WSS, which RC-006b deliberately kept out of the counter.

So a companion cannot inflate capacity today. Rather than add a guard that
would imply otherwise, the invariant is asserted
(`test_capacity_is_unaffected_because_the_companion_never_chats`) so the
property is protected if either side ever changes.

## `snapshot` now lists real availability

It returned `[]` from RC-006a until now, because nothing could exist. Two
filters:

* **Only shares whose owner enabled assistance.** `allow_requests` defaults
  False — sharing a screen does not advertise it to the room. Without this,
  the snapshot would turn every share into an invitation.
* **Only the availability view.** `public_availability` omits the capture
  descriptor: display geometry is a fingerprint of someone's desk, not a
  room-wide fact. The owner reads their own through
  `GET host-bindings/{id}/`.

The per-room index backing it is a **hint, never the truth** — ids are
resolved through `get()`, so a lapsed or terminal binding drops out on read
instead of depending on the index being cleaned up correctly. An index
trusted directly would advertise shares that no longer exist.

## What this does NOT do

* **No LiveKit integration test.** The guard is exercised against
  synthesised webhook events, the same shape `livestream/tests.py` uses.
  There are still no LiveKit credentials in this environment.
* **`livestream/tasks.py` is untouched.** Its reconcile sweep closes
  intervals for participants LiveKit still reports; since a companion never
  opens an interval, there is nothing for the sweep to close. Worth
  re-checking if RC-009's successors make the companion's presence
  stateful.

## Phase 1 is complete

RC-002, 005, 006, 007, 008 and 009 are done. The next task is **RC-010**,
which is Phase 2 and lives in the frontend repositories, not here.
