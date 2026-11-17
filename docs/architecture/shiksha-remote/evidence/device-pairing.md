# RC-007a evidence — DPoP proofs and the device pairing handshake

Date: 17 November 2026. Branch: `docs/shiksha-remote-plan-on-dev`,
cherry-picked to `-on-main`.

**RC-007 is split.** 7a is the cryptographic core and the pairing handshake
— everything needed for a desktop to become a paired device holding its
first credentials. **7b is credential *life*:** refresh rotation with reuse
detection, `devices/` list, `devices/{id}/` unlink, `device-heartbeat/`, and
the device branch of the WS ticket. The split is at ~1,100 lines, past the
workspace's ~800 rule, and it falls on a clean seam: 7a mints a credential,
7b governs what happens to it afterwards.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/dpop.py` | RFC 9449 proof verification (**new**) |
| `sessions_app/collaboration/devices.py` | pairing lifecycle, keyed hashing, device access tokens (**new**) |
| `sessions_app/collaboration/device_views.py` | the four pairing routes (**new**) |
| `sessions_app/models.py` + migration `0020` | `DevicePairing` |
| `config/settings_base.py` | `COLLABORATION_DEVICE_KEY`, `COLLABORATION_CANONICAL_URL` |
| `sessions_app/urls.py` | mounts the four routes |
| `sessions_app/tests_collaboration_pairing.py` | **49 tests** (**new**) |

    POST pairings/                anonymous + DPoP proof of a new key   201
    POST pairings/confirm/        B, recent auth ≤10min                 200
    POST pairings/<id>/poll/      P (secret in Authorization)           200
    POST pairings/<id>/finalize/  P + local confirmation                200

**Naming divergence, recorded not hidden:** the task names
`device_auth.py`; this ships `dpop.py` (the RFC-9449 verifier) plus
`device_views.py` (the routes), with device authentication itself —
`authenticate_device` — living in `devices.py` beside the token minting it
validates. Splitting the RFC implementation from the routes keeps the part
that needs the hardest review in one file with no Django request handling in
it.

**Migration `0020`, not `0021`.** `sessions_app` tops out at `0019` on this
branch and `0018` upstream; RC-006a's `0021` was in `global_settings`. The
upstream ceiling was checked before `makemigrations` ran, per the standing
rule.

## Results

    sessions_app.tests_collaboration_pairing               49 OK
    sessions_app.tests_collaboration_ws      (RC-006b)     61 OK
    sessions_app.tests_collaboration_policy  (RC-006a)     52 OK
    sessions_app.tests_room_file_hardening   (RC-031)      29 OK
    sessions_app livestream global_settings               498 tests, 4 failures + 1 error
    contracts/test_contracts.py + test_openapi.py         13 + 14 OK
    makemigrations --check --dry-run                      No changes detected

🔴 **The 5 non-passes are the permanent environmental baseline** (no LiveKit
credentials locally), by name: `test_request_creates_participant`,
`test_request_with_group_students`, `test_student_can_request_session`
(`RequestSessionTest`), `test_participant_can_view` (`SessionDetailTest`),
`test_start_session` (`SessionLifecycleTest`). The total went **447 → 498**
and the failure count did not move, which is the actual assertion.

## The security core

🔴 **ES256 only, and the algorithm is never read from the token.** The
header's `alg` is compared to a constant and rejected if it differs; the
signature is then verified with a fixed `algorithms=["ES256"]` list. Both,
deliberately — an HS256 forgery whose "secret" is the public JWK sitting in
the proof's own header defeats any server that trusts `alg`, and
`test_the_alg_is_never_taken_from_the_header` builds exactly that token.

🔴 **`htu` is validated against a configured canonical origin, never the
request.** `Host` and `X-Forwarded-*` are attacker-influenced behind a
proxy, and `htu` is the field that stops a proof captured on one endpoint
being spent on another. An **unconfigured** canonical URL refuses outright
rather than matching everything — pinned by its own test.

🔴 **A rejected proof does not burn its `jti`.** Claiming the nonce last,
after every other check, means an attacker cannot evict a victim's
replay-cache entries by submitting proofs that were always going to fail.

🔴 **The replay cache fails CLOSED**, inverting this repo's house style
(`accounts/revocation.py`, `chat/redis_utils.py` both fail open). An
unreachable cache means we cannot distinguish a fresh proof from a replayed
one, and guessing "fresh" is the entire attack.

🔴 **Keys are identified by RFC 7638 thumbprint**, so the same key under a
different JSON field order is the same device — it cannot register twice.

## T02 — neither half of the handshake is sufficient

The contract row and the threat model both claim a stolen browser session
cannot pair a device. Two test classes exist solely to hold that line:

* `BrowserAloneTest` — confirming with a valid signed-in session creates
  **zero** `PairedDevice` rows, returns no token, and cannot reach finalize
  at all (there is no browser finalize: a cookie is not a pairing secret).
  A session older than 10 minutes cannot even confirm.
* `DeviceAloneTest` — a stolen secret without browser confirmation is a 409;
  a stolen secret redeemed from a *different* key is a 401; finalize without
  `local_confirm` is a 403; a wrong secret is an indistinguishable 404.

⚠️ **`local_confirm` is a claim, not a server-verifiable fact**, and the code
says so rather than implying otherwise. Its value is that an attacker
holding the secret but lacking code execution on the desktop cannot produce
it.

⚠️ **"Recent auth" is weaker than the contract row implies.** The row says
"recent account auth ≤10min"; this reads `UserSession.created_at`, so it
means *signed in* within 10 minutes, not *re-entered a password* within 10
minutes. The platform has no step-up re-auth prompt and RC-011 owns the UI
that would add one. **Recorded as a known weakening, not treated as
equivalent.**

## Mutation sweep

30 mutations. **25 caught, 4 equivalent mutants (confirmed, not assumed),
1 belt-and-braces check, and 2 genuinely weak tests — both found and
fixed.** All files verified byte-identical by hash afterwards.

🔴 **Two weak tests, and the second is a repeat of a mistake this project
keeps making:**

1. `test_the_secret_is_never_accepted_from_the_body_or_query` asserted
   `status_code in (400, 401)` against the poll endpoint. But `parse_body`
   rejects `device_secret` as an unknown field with a **400 before any
   credential is read**, so the assertion held even with a body fallback
   wired into the extractor. It now exercises `_pairing_secret` directly,
   *and* asserts the header form still works — otherwise it would pass
   against an extractor that reads nothing at all.
2. `test_a_user_token_is_not_a_device_token` passed with the `typ` check
   deleted, because a SimpleJWT token is signed with a different key and
   dies on the **signature** instead. The test's stated subject was never
   exercised. A new test isolates it: a token signed with the *device* key,
   valid audience, every required claim, differing **only** in `typ` — plus
   a control asserting the same claims with the right `typ` do decode.

Both were re-run under their mutations and confirmed red.

**The four equivalent mutants were proven, not argued.** Each is guarded
twice — once in the view, once in the service — so a single-layer mutation
cannot change observable behaviour. Removing **both** layers was run
explicitly and each test went red:

| Behaviour | Guards |
|---|---|
| alg confusion | explicit `alg` check **+** fixed `algorithms` list |
| originating-key pinning on finalize | view's `expected_thumbprint` **+** service `compare_digest` |
| single-use redemption | `pairing_from_secret` **+** `finalize_pairing` state guard |
| expiry on finalize | `pairing_from_secret` **+** `finalize_pairing` expiry guard |

The belt-and-braces entry with no test expected: making the DPoP replay
cache fail *open*. Reaching it needs a cache backend that raises, which the
local LocMem backend does not do; it is held by code review and the module
docstring instead of a test that would have to lie about what it exercises.

## Open items

1. ⚠️ **Rate limits are still not implemented** — now across three tasks.
   This one adds `3/hour/IP` and `3/hour/key` on `pairings/`, `10/hour/account`
   on `confirm/`, and `one poll/5s`. **None are enforced.** The code-space is
   32¹⁰ ≈ 10¹⁵ and a pairing lives 5 minutes, so brute force is not the
   acute risk — resource exhaustion is. **Release-gate item, and it should
   be one task covering every route rather than a fourth deferral.**
2. 🔴 **`COLLABORATION_DEVICE_KEY` must be set in production.** Unset, it
   derives from `SECRET_KEY` — deliberately a *derivation* rather than
   `SECRET_KEY` itself, so a forgotten setting does not reuse the
   session-signing secret verbatim. But `deployment-rollout.md` requires a
   genuinely separate key, and nothing enforces it yet.
3. 🔴 **`COLLABORATION_CANONICAL_URL` must match the real public origin** or
   every DPoP proof fails `htu`. It defaults to `https://api.shikshacom.com`;
   dev and any staging must override it.
4. **Device credentials cannot yet do anything.** No route accepts
   `authenticate_device` — it is implemented and tested, but its first
   consumer is RC-007b's `device-heartbeat/`. Additive and unreachable, the
   same shape RC-005 used.
5. 🔴 **Guest bindings remain a contract gap**, unchanged, still blocking
   RC-011's OD-01 story.

## Next

**RC-007b** — refresh rotation with reuse detection (`device-token/`), the
`devices/` list, `devices/{id}/` unlink revoking bindings and grants before
responding, `device-heartbeat/`, and the device branch of `ws-ticket/`. The
consumer from RC-006b already refuses a ticket with no `binding_id` at
exactly the point where that branch lands.
