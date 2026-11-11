# RC-002 evidence — input contract frozen (partial)

Date: 11 November 2026. **RC-002 is partially complete.** The input wire contract is frozen, executable and tested; the OpenAPI document and the JS/Rust validators are not done. Reasons below, stated as blockers rather than omissions.

## Delivered

| Artifact | What it is |
|---|---|
| `contracts/input.schema.json` | JSON Schema 2020-12 for the v1 controller input envelope. `additionalProperties: false` throughout, per-kind body branches, the **101-entry** keyboard allowlist, capability and grant-state enums |
| `contracts/fixtures.json` | **48 golden vectors** — 14 valid, 29 invalid-parsed, 5 invalid-raw — each invalid case carrying the contract *reason code* it must earn. Plus deterministic fake-clock constants |
| `contracts/reference_validator.py` | Normative Python validator. Reads every enum and bound **from the schema** so the two cannot drift |
| `contracts/test_contracts.py` | 13 conformance tests. `python contracts/test_contracts.py` → **13 OK**, no Django, no third-party dependency |

The keyboard allowlist was generated rather than typed: `KeyA`–`KeyZ` (26), `Digit0`–`Digit9` (10), `Numpad0`–`Numpad9` (10), `F1`–`F12` (12) and 43 named keys = **101**, asserted unique. The prose in `realtime-event-contracts.md` is the source; the count is now pinned by a test, because a silently growing allowlist is a capability change wearing a typo's clothing.

## Three defects the tests caught before anything was committed

Recorded because each is a trap a reviewer would otherwise hit later.

1. **A fixture that passed vacuously.** The `uppercase_uuid` negative case built its input as `"00000000-0000-4000-8000-000000000001".upper()` — a UUID with no hex letters, so `.upper()` changed nothing and the "rejects uppercase UUIDs" claim was never exercised. Now uses a UUID containing `a` and `b`.

2. **A size limit that could never fire.** The byte ceiling was checked after parsing. Measured against this contract, the largest *shape-valid* packet is **265 bytes on the motion lane (ceiling 1200)** and **1253 on the reliable lane (ceiling 4096)** — every field is bounded, so a conforming message cannot approach either limit. The check was unreachable code that read like a protection, while the real attack — a multi-megabyte blob that costs CPU to parse before anything rejects it — went unaddressed. Size is now enforced on **raw bytes before decoding**, and the ordering (size → decode → shape) is asserted by test.

3. **`json.loads` accepts `NaN` and `Infinity` by default.** The wire contract forbids non-finite numbers, but a permissive parser would have handed a non-finite coordinate straight to the coordinate mapper. Rejected explicitly at the JSON layer, with a separate check for non-finite values arriving in a natively-built dict that never passed through JSON at all.

## Not done, with reasons

- **`contracts/openapi.yaml`** — not written. The REST surface in `api-contracts.md` depends on the ADR-02 transport decision only for the lease/activation shapes, but RC-002's acceptance requires OpenAPI to cover *every* API table row and for examples to validate; doing that properly is a sitting of its own rather than a tail-end of this one. No partial file was left behind, because a half-populated OpenAPI document is worse than none — producers treat it as authoritative.
- **`shared/src/collaboration/protocol.js`** — **deliberately not created.** `shared/` is a separate repository, currently on branch `feat/account-model-simplification` with an uncommitted modification to `src/shared/ProfileSwitcher.jsx`. The workspace `CLAUDE.md` warns that propagating through `shared/sync.mjs` in that state mixes unrelated work into the diff across every dashboard. Needs a clean `shared/` tree first.
- **`shiksha-remote-companion/crates/protocol/`** — **blocked.** No Rust toolchain on this machine (`cargo` and `rustc` absent). A crate that has never compiled is worse than no crate: it would read as a delivered contract implementation while being unverified. The fixtures are language-neutral and ready for it.

So the acceptance criterion "all three validators accept identical positives and reject identical negatives" is **not met** — one of three exists. What *is* established is that the corpus they must agree on is real, executable and self-consistent, which is the part that had to be right first.

## Dependency note

RC-002's stated dependencies are RC-001 and RC-003. RC-003 remains part-done: its rows 1–2 were resolved by the ADR-02 revision, so the input envelope can be frozen; **row 3 (native screen publish renders in an existing room) is still an open Stop against ADR-03.** Nothing in this freeze depends on it — the envelope's `capture_generation` and `track_sid` are opaque identifiers whose *shape* does not change if capture ownership is revisited. If ADR-03 does change, re-check that assumption before extending the freeze to the capture descriptor.

## Reproduce

```
python docs/architecture/shiksha-remote/contracts/test_contracts.py   → 13 OK
```

Isolated from the Django suite by construction: `docs/` has no `__init__.py`, so `manage.py test` discovery cannot reach these files. Verified, not assumed.
