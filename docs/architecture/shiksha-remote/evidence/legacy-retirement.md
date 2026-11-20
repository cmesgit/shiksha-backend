# RC-008 part 2c — retiring the legacy screen-control path

Date: 20 November 2026. Branch: `rc008-part2`.

RC-008's prompt requires the old teacher→student views become "compatibility
rejection/revoke adapters", and the protocol requires that "legacy
request/respond never mint native authority". This is that change, plus one
hole it turned out to be hiding.

## Delivered

| File | What |
|---|---|
| `sessions_app/remote_control_views.py` | demoted to an adapter |
| `sessions_app/tests_legacy_remote_control.py` | **10 tests** (**new**) |

    → 10 tests, OK

## Why `respond` had to go

`respond_remote_control` moved a grant from `requested` straight to
**`active` on a browser click alone**. Shiksha Remote rests on the opposite
rule: a browser approval reserves a device and nothing more, and only the
companion's locally-operated physical dialog reaches `active`.

Leaving a second, weaker door into the same `active` state would have made
that guarantee depend on which URL a caller chose. Both endpoints now
answer **410** unconditionally — not "under the current configuration".
`test_the_legacy_flag_does_not_resurrect_the_endpoints` exists because
`live_remote_access_enabled` still defaults **True**, and someone reading
only the flag name could reasonably expect it to turn the old path back on.

It was already unreachable: nothing in the codebase sets
`GroupSessionParticipant.is_sharing_screen`, so `request_remote_control`
always answered 409 `not_sharing`. But "unreachable by accident" and
"refused on purpose" are different things, and only the second is a
guarantee.

## 🔴 The hole this closes

`revoke_remote_control` looked a grant up by raw `id` with **no
`schema_version` filter**. A **v1** grant — one belonging to the new
collaboration plane — could therefore be terminated through the legacy
endpoint, which bypasses `collaboration.grants` entirely: no version bump,
no audit row, no notification.

The failure mode is worse than "a write in the wrong place". Both ends keep
rendering live control over a grant the database has already revoked, and
nothing corrects them until the lease expires. A revocation that tells
nobody is barely a revocation.

The lookup is now restricted to `schema_version = 0`, and the tests assert
the v1 grant's **version has not moved** and **no audit row exists** —
not merely that the request 404'd. The host is tested separately from the
target, because the host holds the broadest legacy permission and is the
one most likely to slip past a narrower fix.

## Why `revoke` was NOT demoted

A grant made before this change may still be sitting `active` in some
deployment's database. Withdrawing the only endpoint that can stop it would
be the opposite of safe. **Retiring the ways to start something while
keeping the way to stop it is the correct asymmetry** — the same reasoning
that makes `grants.terminate` ignore the kill flag.

## What remains true

Legacy rows keep `schema_version = 0`. The collaboration state machine
refuses to address them at all (`grants._lock_grant` filters to v1), and
the legacy revoke now refuses v1. The two worlds cannot reach into each
other in either direction, which is the property that lets both exist in
one table.
