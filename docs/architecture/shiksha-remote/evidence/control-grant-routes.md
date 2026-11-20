# RC-008 part 2d — the control-grant HTTP surface

Date: 20 November 2026. Branch: `rc008-part2`.

The nine routes from `api-contracts.md` §Control grants. Parts 2a and 2b
built the machine and the crypto; until now neither was reachable over
HTTP, and everything was exercised only from tests.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/grant_views.py` | the nine routes (**new**) |
| `sessions_app/urls.py` | wiring |
| `sessions_app/tests_collaboration_grant_routes.py` | **23 tests** (**new**) |

    → 23 tests, OK (33 with part 2c's)

## Two planes on one resource

Browser routes take cookie auth + CSRF + an exact Origin. Device routes take
a device access token + a DPoP proof and **consult no cookie**. The three
shared routes (`GET`, `pause`, `terminate`) dispatch on the credential
actually presented rather than on a parameter, so a caller cannot choose
which plane validates it.

🔴 **This separation is the property the whole consent design rests on.** If
a signed-in user could reach `activate`, "only the companion can open the
gate" would be false. `PlaneSeparationTests` asserts it from both
directions, including that a bare `Bearer` token is not a device credential
— `authenticate_device` requires the `DPoP` scheme specifically, so a token
lifted into an ordinary header buys nothing.

**Every device-plane test mints a real access token and signs a real ES256
DPoP proof**, reusing RC-007's helpers. Nothing is mocked. Part 2b is the
reason: its seam-ordering bug passed every refusal test while the path it
guarded could never once succeed, so the happy paths are asserted first
here and the refusals after.

## 🔴 POST grants/ cannot succeed yet, and the task graph does not show why

The create route takes `{controller_binding_id, host_binding_id,
capabilities}`. The controller comes from the authenticated principal; the
**target and the device are derived from the host binding** — the contract
is explicit that there are "no client controller/target IDs", because a body
that could name its own target would let anyone push a consent prompt at
anyone.

Host bindings are **RC-009's**, and they do not exist. So there is nothing
to derive from, and the route refuses with the contract's own
`capture_not_ready`.

**The dependency graph says RC-009 depends on RC-008.** At the service layer
that is true and this work proves it: `grants.request_control` is complete
and tested. But at the HTTP layer the create *route* depends on RC-009, and
the graph does not record that edge. Anyone sequencing frontend work from
the graph alone would plan against a route that cannot return 201.

The refusal is deliberately *not* the first thing the view does: the
caller's own binding is validated first, so the route cannot be used to
probe whether a host binding exists. `_resolve_host_binding` is a named
function with the reasoning beside it, so what RC-009 replaces is greppable.

## Decisions worth knowing

**The lease route is exempt from idempotency.** Every other mutation
requires a UUID `Idempotency-Key` and replays the stored result; the lease
must not, because replaying a cached lease is exactly what a three-second
deadline exists to prevent. The contract says so in its lease row, and
`test_a_device_can_renew_a_lease_without_an_idempotency_key` pins it.

**`terminate` requires `expected_version` and ignores it.** The contract
lists the field; the protocol says "if version stale still revoke current
resource after auth (stop wins)". Both are satisfied by reading it and not
enforcing it. A client that omits it is told, rather than silently getting a
different concurrency model from every other mutation.

🔴 **A possible contract tension, for the owner rather than for me.**
Requiring an `Idempotency-Key` on `terminate` means a stop with no header
is a 400. Stop is naturally idempotent, so the key buys nothing there, and
the one operation whose entire job is to stop control now has an extra way
to fail. I implemented the contract as frozen rather than deviating —
"resolve changes by the designated contract owner, not by letting consumers
accept arbitrary alternatives" — but it is worth a decision.

**No lease or ticket ever reaches a browser.** `activate` and `resume`
answer a device and only a device. `SerialisationTests` asserts the grant
body contains no `consent_nonce_hash`, no `activation_jwt`, no `lease_jwt`,
no `auth_session_id` and no tracked session id — the context snapshots are
reduced to a bare context name, because a profile id and an auth session id
are participant secrets rather than grant metadata.

**URL order is load-bearing.** The bare `<id>/` detail route is declared
**last**: "decision", "lease" and the rest all parse happily as
`<str:grant_id>`, so a detail route declared first would swallow every
action route. Tested.

## What this does NOT do

* **`POST grants/` returns 409 until RC-009.** See above.
* **No host-binding freshness on renewal.** Still RC-009's, still
  `leases._host_bindings_exist`.
* **Both flags ship off**, so every route answers 404 in any current
  deployment.
* **No frontend.** RC-010 and RC-011 own that.

## Next

RC-009 — companion room lifecycle and capture bindings. It closes three
things this pass had to leave open: the create route's target resolution,
the host half of the 3s freshness rule, and `snapshot`'s permanently empty
`host_bindings` list.
