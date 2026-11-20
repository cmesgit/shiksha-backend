# RC-008 part 2b — activation tickets and bounded leases

Date: 20 November 2026. Branch: `rc008-part2`, on top of part 2a.

Part 2a built the consent state machine. This adds the two signed artefacts
that turn an `active` row into something a companion can act on, and the
rules that stop either outliving its consent.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/leases.py` | ticket + lease minting (**new**) |
| `sessions_app/tests_collaboration_leases.py` | **30 tests** (**new**) |
| `sessions_app/collaboration/bindings.py` | heartbeat stamp + freshness rule |
| `config/settings_base.py` | `COLLABORATION_CONTROL_SIGNING_KEY` |

    DJANGO_SETTINGS_MODULE=config.settings_test venv/bin/python \
      manage.py test --noinput sessions_app.tests_collaboration_leases
    → 30 tests, OK

## The division that is the whole design

The **activation ticket** (`shiksha-control+jwt`, EdDSA/Ed25519) says *this
pairing of controller, target, device, room and capabilities was consented
to*. It lives as long as the grant — up to ten minutes.

The **lease** (`shiksha-control-lease+jwt`) says *and it is still true, one
second ago*. It lives at most three.

🔴 **The ticket's `exp` is not a permission to inject**, and the protocol
says so in as many words. If that ever inverts, worst-case revocation
latency becomes the ticket lifetime instead of the lease deadline — ten
minutes instead of three seconds. That is the difference between this
feature being safe and not, so `test_a_lease_lasts_at_most_three_seconds`
and `test_a_forged_longer_lease_is_refused` both exist to pin it.

## 🔴 A gap in RC-006 that blocked the lease rule

The protocol requires the last binding heartbeat be **≤3s old** for a lease
renewal, *even though Redis retains the record for 6s*. The gap is
deliberate: the TTL is how long a binding stays usable, the 3s is how fresh
it must be to keep authorising input, and without the gap a participant who
vanished keeps control for a full TTL.

**There was nothing to measure.** `bindings.renew` re-wrote the record at a
full TTL but never stamped *when* — so a record one second from expiry and
one just renewed were indistinguishable to any reader. `renew` now stamps
`heartbeat_at`, and `heartbeat_age` treats a record lacking the field as
**maximally stale** rather than fresh. That matters during a deploy: the
only records without the stamp are ones nobody has heartbeat since, and
guessing "fresh" there would authorise input on the strength of a missing
field.

## 🔴 A bug that would have shipped the whole path as dead code

`_assert_bindings_fresh` originally tested "did this binding lapse?" before
testing the RC-009 seam. Host bindings do not exist yet, so a grant's
`host_binding_id` is a placeholder with no record — the lookup always
missed, and **every lease was refused `binding_lapsed`**.

What makes this worth writing down is how it presented: every *refusal*
test passed, because they all refuse earlier for their own reasons. Only
the four tests that expected a lease to be minted failed. A suite that
tested only the negative cases — which is the natural instinct for security
code — would have gone green over a feature that could never once succeed.

The seam is now checked first, and is a named function (`_host_bindings_
exist`) rather than a bare `False`, so what RC-009 has to change is
greppable.

## Decisions worth knowing

**Asymmetric, not symmetric.** `COLLABORATION_DEVICE_KEY` is an HMAC key the
server uses to recognise its own device tokens. Control tickets use an
**Ed25519** key instead, because a companion must be able to *pin and
verify* one without holding any secret of ours. Unset, it derives from
`SECRET_KEY` — same philosophy as `devices._server_key`, and stable across
restarts so a redeploy does not invalidate outstanding tickets. 🔴 Rotating
`SECRET_KEY` while it is unset kills every live grant; production must set
it.

**`kid` is carried so an unknown key can be *rejected*.** The protocol
requires rejecting unknown algorithms and key ids; that is only meaningful
if tickets carry one. Tested from both directions — a stranger's key with
its own kid, and an HMAC token offered where an asymmetric one is expected
(the classic JWT confusion, which a fixed algorithm list is what prevents).

**`lease_seq` lives in the cache, never a column.** At one lease per second
per active control, a column would make this a row write per second per
grant, and the contract says the box dies around twenty concurrent
controls. `cache.incr` is atomic on Redis. A duplicate seq is not cosmetic:
the companion's rule is "reject lower **or equal**", so a duplicate makes it
discard a legitimate renewal and pause.

**The device row is locked, not written.** `SELECT ... FOR UPDATE` on the
device is what the contract means by "per-device serialization locks prevent
a lease from committing after revoke", and it satisfies the no-row-write
rule at the same time.

**Every renewal re-reads authority from the database** — state, version,
epoch, device revocation, binding freshness and the kill flag — never from
the ticket and never from a cached decision. A worker holding a pre-revoke
decision is precisely how a revoked grant keeps injecting.

**The store being unreachable is a refusal, not a pass.** "Backend
dependencies unavailable means no renewal." Failing open would keep input
flowing during exactly the outage that stops us noticing a disconnect.

## What this does NOT do

* **No routes.** Nothing in `urls.py` reaches either module. `mint_lease`
  and `mint_activation` have no HTTP surface yet; `POST grants/{id}/lease/`
  and the rest of `api-contracts.md` §Control grants are still unwired.
* **`remote_control_views.py` is still untouched.** Its demotion to a
  compatibility rejecter remains outstanding from part 2's original scope.
* **The host side of the 3s rule is not enforced**, because there is nothing
  to enforce it against. A grant whose *host* vanished is currently caught
  by the binding-deletion cascade and by LiveKit SID verification, neither
  of which is the 3s bound. **RC-009 must close this**, and
  `_host_bindings_exist` is where.
* **No verifier runs on a companion.** `verify_activation` and
  `verify_lease` exist for the tests and as a reference; the companion's
  real check is stricter and cannot live here — its deadline is
  `min(request_start + 3s, exp)` on a **monotonic** clock measured from when
  it sent the request, so a slow reply shortens the window rather than
  extending it. A server-side verifier can only see `exp`.

## Next

The rest of RC-008: demote `remote_control_views.py` to a compatibility
rejecter that can never mint native authority, and wire the routes. Then
RC-009, which makes `host_binding_id` mean something and closes the host
half of the freshness rule.
