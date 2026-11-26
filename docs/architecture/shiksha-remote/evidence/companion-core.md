# RC-012 — native authorisation core (part 1): written and type-checked, tests NOT RUN

**Status: partial.** The authorisation core exists and compiles. **None of its
tests have been executed**, and two of the three crates have not been compiled
at all. Read "What was and was not run" before relying on anything here.

Workspace: `shiksha-remote-companion/` at the repo root of this branch.

## What was and was not run

Machine: Windows 11 Home, Rust 1.100.0 (rustup, `stable-x86_64-pc-windows-msvc`,
installed 26 Nov 2026; `rustup-init.exe` SHA-256 checked against the published
`.sha256` file before running).

| command | result |
|---|---|
| `cargo check -p shiksha-companion-core --all-targets` | **pass**, 0 warnings |
| `cargo test -p shiksha-companion-core --no-run` | **pass** — unit and integration test executables compile and link |
| executing any test binary | **not run** — blocked, see below |
| `cargo check` / `cargo test` on `shiksha-companion-transport` | **not run** |
| any build of `shiksha-companion` (app) | **not run** |
| mutation sweeps | **not run** |
| fuzzing | **not run** |

**Why nothing executed.** Windows Application Control on this machine blocks
unsigned executables: running a freshly built `hello world` fails with
`An Application Control policy has blocked this file (os error 4551)`. Two
consequences:

1. No test binary can run. `cargo test` compiles but cannot execute.
2. `cargo check` on any crate whose dependency graph contains a **build script**
   fails, because Cargo must *run* `build_script_build`. `quote` and
   `proc-macro2` (under `serde_derive`, `thiserror`, …) have one. So only a
   dependency-free crate can be checked here.

The policy was not worked around. That is why `shiksha-companion-core` has an
empty `[dependencies]` table — a design choice that happens to be what makes it
checkable here — and why `transport` and `app` are written but unverified.
Cargo did resolve their dependency graph (`Cargo.lock` is committed), so the
crate names and version requirements exist; whether the code compiles against
them is unknown.

## What exists

| crate | lines (src+tests) | state |
|---|---|---|
| `core` | ~3,500 | checked; 31 unit + 51 integration tests **compiled, not run** |
| `transport` | ~600 | **unverified**: `codec`, `jwt`, `entropy`, and a fixture test (5 tests) |
| `app` | ~80 | **unverified**: composition root, idle only |

### `core` (std-only, no I/O)

| module | owns |
|---|---|
| `wire` | typed envelope, 101-key allowlist, semantic validation, reject vocabulary |
| `ticket` | header check (EdDSA only, pinned `kid`, `typ`), activation validation against what the owner approved |
| `lease` | outstanding nonce, 1 s reply cap, `min(request_start + 3 s, server span)` monotonic deadline, strictly increasing `lease_seq` |
| `companion` | consent latch, the 9-condition gate, sender and sequence checks, freshness challenges, bounded queue, rate buckets, the single injection path, held-input ownership, end-of-authority ordering |
| `adapter` | `InputAdapter` trait; `ValidatedEvent` whose constructor is `pub(crate)`; `FakeInputAdapter` |
| `clock` | `Mono` monotonic time, `FakeClock`, `Entropy` trait, base64url |

## Decisions made, and why

- **core takes no dependencies.** Signature verification, JSON and randomness
  are injected by `transport`. Beyond making it checkable here, it means every
  authorisation decision runs under a fake clock and a fake OS adapter.
- **A pause ends the epoch.** There is no paused state in the companion. The
  protocol says paused cannot survive a reconnect and resume needs a fresh
  epoch and local consent; modelling a resumable pause would have invited a
  bug where it does survive. Coming back is a new `activate` with physical
  consent.
- **Physical provenance is a parameter.** `activate` and `approve_share` take
  `InputOrigin`; `Injected` is refused. The Windows adapter (RC-013) must supply
  it by marking its own `SendInput` events. Until then that guarantee is only as
  good as the caller — **this is T13's open half**.
- **The gate opens on the first accepted lease, not on activation.** `ready` and
  the first challenge are emitted then. So there is one extra round trip between
  Allow and the controller being told it can start, and a grant that never gets
  its first lease is abandoned after 3 s.
- **Wall clock is read once.** To turn the ticket's `exp` into a monotonic
  deadline at activation. Lease timing uses only `exp − iat` (a duration) and
  local monotonic time, so a clock jump has nothing to act on. This property is
  by construction; no test pins it (an earlier test claiming to was removed
  because it could not fail).
- **Status is addressed to one identity.** `Outbound::Status` carries
  `to_identity`, and `transport::codec::deliver` refuses an empty destination.
- **Never-injectable chords** (Ctrl+Alt+Delete, Meta+L, Ctrl+Alt+Shift+F12) are
  judged against the *resulting* pressed set at injection time.

## Applicable security-test-plan cases

Covered by tests that compile but have not run: T04 (stranger/server-origin
packets), T05 (old/replayed packets, late lease), T06 (wrong room/device/SID/
capture), T08 (lost revoke → lease lapse, late reply), T10 (every reconnect/
sleep/lock/display event), T13 (injected consent refused — core half only),
T14 (flood, malformed run, queue overflow), T20 (sequence gap, duplicate,
reordered lossy move, heartbeat release).

**Not covered:** T09 (process kill while holding a key — needs the
watchdog/helper, RC-013/014), T11, T12, T15 (needs real geometry), T22, T23,
T24 (server/browser side or safeguarding review).

## Second-pass review (same day, still no execution)

Every integration test and the unit tests in `wire`, `lease`, `ticket`, `clock`
and `companion` were traced by hand against the code. Result: all traced as
passing except where noted, and **one real bug in the code**: the heartbeat
deadline was set at activation, so a slow first lease (legal up to 1 s) left the
controller almost no time for its first heartbeat and a healthy grant would end
as `heartbeat_timeout`. Fixed (deadline now restarts when the first lease opens
the gate) with regression test
`a_slow_first_lease_does_not_eat_the_controllers_first_heartbeat_window`.

Hand-tracing is weaker than running: it checks the model in my head, and the
bug above survived the first pass. Still unverified: `transport` and `app`
entirely (including that the fixture vectors map to the reasons the decoder
returns).

## Known weaknesses

- **Unrun tests were desk-checked, not executed.** Reading them against the code
  found and fixed several defects (heartbeat deadline ending grants inside
  lease-timing assertions; a helper that panicked after authority ended; a test
  that could not fail). There are almost certainly more. Expect some to fail on
  first run; a failure is as likely a test bug as a code bug.
- **The 10 s held-input limit is unreachable through the public API.** Without a
  heartbeat confirming a hold, the 1 s heartbeat timeout ends the grant first.
  It is defence in depth; it has no test.
- **`host_sid` and `controller_binding_id` from the ticket are not checked**
  against anything; the core has no source of truth for them.
- **`applied_seq` in acks is approximate:** the last in-order accepted sequence
  when the queue is empty, else the last executed one.
- **No Tokio.** Queues are bounded and rate-limited inside the core, called from
  the app's threads. The architecture doc's "native UI thread, network tasks,
  capture worker, single serialised executor" split is not built; the app has
  one watchdog thread.

## Not done in RC-012

DPoP API client; key storage (DPAPI/TPM); credential refresh; LiveKit join,
data-channel receive and status send, and the SDK-event mapping onto
`TransportEvent`/`Inbound` (needs `livekit` 0.12 with
`rustls-tls-native-roots`, `+crt-static`, and a machine that can run it);
native consent UI (RC-014); Windows capture/input adapter (RC-013).

## To pick this up on a machine that can execute binaries

1. `cd shiksha-remote-companion && cargo test --workspace`. Fix transport
   compile errors first; then judge each failing core test on its merits.
2. **Mutation-check before trusting green** (HANDOFF §8), in the background.
   Suggested breaks, each should turn specific tests red: drop the latch
   comparison in `Companion::gate`; compute the lease deadline from receipt time
   in `LeaseTracker::accept`; remove the `Physical` check in `activate`; remove
   the sender comparison in `on_packet`; change `seq <= reliable_last` to `<`;
   skip the gate re-check in `pump`; drop the `physically_held` guard in `end`.
3. Then RC-013 (Windows adapter) — it must mark its own injected events.
