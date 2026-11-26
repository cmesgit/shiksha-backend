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
| `core` | ~3,500 | checked; 94 unit + 55 integration tests **compiled, not run** |
| `transport` | ~2,400 | **unverified**: `codec`, `jwt`, `entropy`, `session`, `keys`, `http`, `device_api`; 35 tests (5 fixture, 12 session, 18 device API) |
| `livekit` | ~250 | **unverified**: SDK glue, no tests (needs a room) |
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

## Part 2 — LiveKit join and status wiring (written, never compiled)

Layered so the SDK-specific surface is as small as possible:

| where | what | checkable here |
|---|---|---|
| `core::Companion::on_malformed` | decoder-refused packets count toward the same 16-run limit, **only** when from the bound controller; strangers and server-origin get no reply and no effect | yes (compiled; 1 new test) |
| `transport::session` | `Driver`: neutral `NetEvent` in → core → addressed `Packet`s + server notices out; topic filter; `lower()` drops an empty destination and counts it | no |
| `crates/livekit` | `join(JoinPermit,…)`, `to_net_event`, `publish` (one identity), `run` loop, `watchdog_step` | no |

Decisions:

- **Join takes a `JoinPermit`** (consumed). After connecting it checks the room
  name equals the approved room and the local identity equals
  `rcdev_<host_binding_id>`, and leaves if not — the token's own claims are the
  server's check, these are ours.
- **Media isolation is enforced here, client-side.** `auto_subscribe = false`,
  and `to_net_event` handles no track/publication event, so no sink exists.
  The token grants `can_subscribe: true` (ADR-02 Option C), so this is the only
  place that isolation lives; do not add track handling without revisiting T23.
- **Publishing is one identity per packet**, refused when empty in both `lower`
  and `publish`.
- **Status is best-effort and bounded**: the watchdog uses `try_send`, so a wedged
  publisher drops status instead of blocking the thread that enforces deadlines.
  The gate is always closed *before* a `stopped` is produced.
- **A poisoned driver lock closes the gate** and continues.
- TLS: `rustls-tls-native-roots` selected explicitly (HANDOFF §5).

SDK facts used, read from docs.rs on 26 Nov 2026 (version **0.12.0** pages;
Cargo resolved **0.12.1**, so a signature may have moved): `Room::connect(url,
token, RoomOptions) -> RoomResult<(Room, UnboundedReceiver<RoomEvent>)>`;
`RoomOptions` is `#[non_exhaustive]` with public `auto_subscribe`,
`adaptive_stream`, `dynacast`; `RoomEvent::DataReceived { payload:
Arc<Vec<u8>>, topic: Option<String>, kind, participant: Option<RemoteParticipant>
}`; `DataPacket { payload, topic, reliable, destination_identities }`;
`LocalParticipant::publish_data(&self, DataPacket) -> RoomResult<()>`;
`ParticipantIdentity(pub String)`, `ParticipantSid` readable via `as_str()`;
`Room::name() -> String`. **Not verified:** the default values of
`RoomOptions` (irrelevant to the three fields we set explicitly), and anything
about how `RoomEvent::Disconnected`/`Reconnected` interleave with `DataReceived`
under load.

Still open in RC-012 after this part: the DPoP API client, key storage,
credential refresh and the token fetch that feeds `join`; and **composition** —
`app` does not yet build a `Driver`, spawn the `run` task or the watchdog.
Screen publish is RC-013.

## Part 3 — DPoP client, credentials and device API (written; core half checked)

| where | what | checkable here |
|---|---|---|
| `core::dpop` | proof assembly: canonical JWK, thumbprint input, `ath` only on resource routes, `htu` rules, clock offset; hashing and signing injected | yes (compiled; 11 tests) |
| `core::credentials` | refresh state machine (below) | yes (compiled; 19 tests) |
| `core::Companion::{fresh_nonce, adopt_activation_lease}` | first lease from the `activate` reply, timed from the request's start | yes (3 tests) |
| `transport::keys` | `P256Key` (`p256`), `Sha256Hasher`, `SecretStore` + `MemoryStore`, `load_or_create` | no |
| `transport::http` | `Http` trait; `ReqwestHttp` (rustls, OS roots, **no redirects**) | no |
| `transport::device_api` | pairing, refresh, heartbeat, share, ready, activate, lease, terminate; `classify_refresh` | no (18 tests) |

### What the server does that the contract does not say (read from `devices.py`)

1. **Refresh tokens are single-use and reuse revokes the device**, with no grace
   window (`rotate_credentials`).
2. **Rotation bumps `credential_version`, which invalidates every outstanding
   access token.** A refresh during an active grant makes the next lease 401
   until the new token is in use. Lease deadlines (3 s) bound the damage but the
   lease loop must tolerate one 401 and wait out the refresh.
3. **The proof is verified before any credential logic**, so 401
   `device_proof_invalid` is a definitive "nothing rotated" — safe to retry after
   correcting the clock.

Policy that follows, encoded in `credentials.rs` and `classify_refresh`:

- never two refreshes in flight (the loser is treated as reuse);
- an **ambiguous** outcome (timeout after send, 5xx, an unreadable 200) waits 60 s
  and retries **exactly once**. If rotation happened the device was unrecoverable
  anyway; if not, the retry saves the pairing. A second ambiguity ends in "pair
  again";
- the rotated refresh token is handed back as `Effect::PersistRefresh` and the
  state machine will not start another refresh until the save is confirmed; a
  failed save is a hard failure (the server's copy has moved on, ours is lost);
- only four 401 codes are treated as "dead" (`credential_invalid`,
  `credential_expired`, `device_revoked`, `device_unknown`). Any other failure is
  ambiguous rather than fatal, so a transient fault cannot erase a good
  credential and force a re-pair;
- access-token expiry runs from when the refresh was **sent**;
- a crash mid-refresh is recognised at next start by a marker written before the
  request (`REFRESH_INFLIGHT_NAME`), and goes through the same one-retry path.

### Contract vs implementation (the implementation is what the client follows)

| item | contract | server code |
|---|---|---|
| `device-token/` body | `{refresh_token}` | **also requires `device_id`** |
| device routes | all non-heartbeat mutations need `Idempotency-Key` | none of the device routes use it; `lease/` must not |
| DPoP server nonce | architecture text mentions one | not implemented |
| `activate` reply | `{grant, activation_jwt, lease_jwt}` | the lease is bound to the request's `lease_nonce`, which the core now adopts |

### Decisions

- **Hashing/signing are injected into core.** What stays in core is everything a
  reviewer can get wrong around the crypto: the signed bytes, the canonical
  thumbprint input, which claims appear on which route. `sign_es256` must return
  64-byte `r||s`, not DER.
- **Never follow redirects.** A hop would re-send `Authorization` and a proof to a
  host we did not choose.
- **HTTP errors split "not sent" from "may have been".** Only connect/DNS/TLS
  failures are `NotSent`; a timeout is deliberately not.
- **Clock skew:** the server's time is learned from every reply's `Date` header
  and applied to `iat` only, never to a monotonic deadline.
- **TLS:** `reqwest` 0.12.28 resolved with `ring` (no `aws-lc-sys`, so no
  CMake/NASM) and `rustls-native-certs`, so the OS trust store is honoured.

### Open — and the first is release-gating

- ~~No OS-protected `SecretStore` exists~~ — a DPAPI one now exists (Part 4). It
  is the floor, not the ceiling: see its limits there.
- The pairing *flow* (poll loop with the 5 s interval, the local "does this
  account match?" confirmation, then `finalize`) is a set of API calls, not a
  driver; the native UI that asks the owner is RC-014.
- Heartbeat scheduling, the host-binding lifecycle driver and the grant/lease
  driver that calls `lease` every second and survives a refresh 401 are not
  written, nor is the composition in `app`.
- `reqwest`'s `is_connect()` classification (what counts as not-sent) was written
  from memory of the API and is the single most consequential unverified line in
  the HTTP layer: a mistake there turns a timeout into a retry, which can revoke
  a device.

## Part 4 — DPAPI secret store (written; type-checked, never executed)

`SecretStore` moved into `core::secrets` (std-only) so `transport` and the new
`crates/platform-windows` can share it without depending on each other.
`DpapiStore` keeps each secret as a DPAPI blob in
`%LOCALAPPDATA%ShikshaRemotesecrets<name>.dpapi`.

**Checked:** `cargo check -p shiksha-companion-platform-windows --all-targets` and
`cargo test --no-run` both pass, and the test executable *links* against
`crypt32`, so the three hand-declared Win32 signatures (`CryptProtectData`,
`CryptUnprotectData`, `LocalFree`) at least resolve. The crate has no
dependencies beyond core and no build scripts, which is why it can be checked
here. **Not run:** every test, including the 7 that exercise real DPAPI.

Behaviour the callers rely on:

- `get` is `Ok(None)` **only** when the file does not exist. A present file that
  will not decrypt (corrupt, another user, another machine, swapped name) is
  `Err`, because `None` would make `load_or_create` mint a fresh device key and
  orphan the pairing.
- `put` writes a temp file in the same directory, `sync_all`s it, then renames
  over the target: a crash leaves the old value or the new one. This is what the
  refresh policy needs ("persist before use"). `std` has no directory sync on
  Windows, so the rename's own durability is the OS's.
- Each blob carries a per-name entropy, so a file copied to another name will not
  decrypt. Names are `[a-z0-9-]{1,64}` and become file names with no escaping.
- No fallback location: a missing `%LOCALAPPDATA%` is an error, never the working
  directory or a temp folder.
- DPAPI output is copied then zeroed before `LocalFree`.

**What this does and does not protect.** DPAPI binds the data to the signed-in
Windows user, so the files are useless to other users, to an offline reader of
the disk, and to a profile copied to another machine. 🔴 It does **not** stop other
code running as the *same* user, which can call `CryptUnprotectData` on the same
files; the per-name entropy is a constant, not a secret. The stronger design is a
**non-exportable key in the TPM** (CNG, Platform Crypto Provider) so the private
key never enters process memory. That is not done and would change `P256Key`
into a handle that signs rather than bytes that sign. Treat DPAPI as the minimum
that satisfies "OS-protected storage", and decide deliberately whether the TPM
design is required before the pilot.

Unverified: whether `CryptProtectData` accepts a zero-length input (the
`empty_and_large_values_round_trip` test will say; nothing we store is empty).

## Part 5 — drivers and composition (decision logic checked; I/O not)

The loops are split so the *decisions* are type-checked and the *I/O* is thin.

| where | what | checkable here |
|---|---|---|
| `core::drivers` | `PairingFlow`, `HeartbeatSchedule`, `LeaseDriver`, `classify_lease_error`, `activation_plan` | yes (28 tests compiled) |
| `transport::runtime` | `pair()`, `DeviceWorker` (refresh + heartbeat), `LeaseWorker`, `activate_grant()` | no |
| `app` | CLI: `--pair`, or restore credentials and run the worker + watchdog | no |

Rules that live in core, each with tests:

- **Pairing polls no faster than 5 s** (the server's own floor, also a floor on
  the interval it reports), one in flight, doubling backoff capped at 30 s.
  The 5-minute challenge is measured from when the create request was *sent*,
  and is checked in **every** live phase — including while waiting for the owner,
  which the first draft missed (it could wait forever).
- **Only a physical answer counts** for the "does this account match?" question;
  an injected one is refused, changes nothing, and the question is asked again.
- **Finalize is never retried once it may have redeemed the secret** (redemption
  is atomic and one-time): a lost reply ends in "pair again". Only a provably-
  not-sent failure retries, without asking the owner again.
- New credentials are persisted before they are handed over; a failed save marks
  them `StoreFailed` but keeps the session alive.
- **Lease loop:** once a second, one at a time, scheduled from the request's
  *start*. A 401 for a dead token → refresh; `device_revoked`, 404, 409, 410 →
  authority gone, end the grant; everything else (429, 5xx, clock-skew 401s) →
  do nothing and let the 3 s deadline be the safety net. Unknown 409s count as
  gone (closing is the safe direction); unknown anything else does not.
- **Activation failure plan:** terminate with `protocol_error` exactly when the
  server may believe the grant is active but we will not act on it (ambiguous
  send, or a reply that fails signature/validation). `NotSent` may reuse the
  same physical consent; a definitive refusal may not.

Threading (in `runtime.rs`): the device worker may block ~20 s on HTTP; the
lease worker has its own client with a 1.5 s timeout so a stuck refresh or
heartbeat can never delay a lease. No lock is held across HTTP. A poisoned
driver lock closes the gate. `SystemClock` is now `Copy` and must be created once
and copied: `Mono` values from two clocks are not comparable.

Defects the type-check and desk review caught this pass (so: expect more):
`on_finalize` read the request time from the wrong field; `activate_grant` dropped
the `ready`/challenge status; a refused non-physical answer left `pair()` waiting
forever; a failed save discarded a live session.

**Not built — the app is a pairing + heartbeat shell.** There is no share flow
(`approve_share` → token → join → publish → `ready`), no live grant flow driving
`activate_grant`, and no LiveKit composition: those need the capture/input
adapter (RC-013) and the native consent UI (RC-014). `--pair` confirms on the
**console**, which reports itself as physical and cannot prove it; it is a
development stand-in and must be replaced, not shipped. The lease worker's
`grant_id` is never set by anything yet.

## Part 6 — RC-013 part 1: Windows input, display geometry, physical-input monitor

**Capture (`capture.rs`, WGC) is NOT started.** It needs the `windows` crate and
the LiveKit `NativeVideoSource`, both behind build scripts this machine blocks, so
it could not be type-checked here and was not guessed at. Everything below is the
rest of RC-013, and it is **type-checked** (and partly *executed at compile time*).

New in `crates/platform-windows`, hand-declared Win32 FFI, no dependencies beyond
core:

| module | what |
|---|---|
| `keymap` | DOM `code` → set-1 scan code (+ extended flag), all 101 allowlisted keys |
| `display` | `(u,v)` → physical pixel → `SendInput` absolute; monitor enumeration, DPI, rotation; descriptor JSON + range validation; topology fingerprint; per-monitor-v2 enforcement |
| `input` | `WindowsInput`: the `InputAdapter` over `SendInput`, behind an `InputSink` trait |
| `provenance` | low-level keyboard/mouse hook that tells owner input from injected input |

And in core: `TransportEvent::PhysicalActivity` → `StopReason::LocalActivity`
("local input priority": the owner touching the machine ends the grant and
releases remote-owned keys).

### What was verified by rustc itself (`const _: () = assert!`, 44 of them)

These run **inside the compiler**, so they hold on a machine that cannot execute a
single test binary. Each was mutation-checked: I broke the code and confirmed the
build failed with the right message.

- every one of the 101 allowlisted keys has a scan code; no two share one or a DOM
  code; the map holds nothing the protocol does not allow; `Numpad8` and `ArrowUp`
  differ only by the `E0` prefix. *(Mutations: duplicate scan code → build fails;
  allowlisted key swapped for `PrintScreen` → build fails.)*
- `INPUT` is 40 bytes on x64 (28 on x86), `MOUSEINPUT` 32, `KEYBDINPUT` 24 — the
  exact trap RC-003 paid for (a 56-byte `INPUT` makes `SendInput` return 0 with
  error 87). `DEVMODEW` is 220 bytes with `dmDisplayOrientation` at offset 84 and
  `dmPelsWidth` at 172; `MONITORINFOEXW` 104; the hook structs 24/32.
  *(Mutation: drop one `DEVMODEW` field → both size and offset asserts fail.)*
- the contract's pixel mapping `x = left + round(u·(width−1))` at the places an
  off-by-one shows first: `u=1` is the last pixel, a display to the LEFT of
  primary (`u=1` → `-1`), out-of-range clamped. *(Mutation: `size` instead of
  `size−1` → fails.)*
- wheel arithmetic: one step = 120, ±10 steps = ±1200, clamped.

### A finding: the "obvious" absolute-coordinate formula is wrong

`SendInput` wants 0..65535 across the virtual desktop; Windows maps that back to a
pixel by an unstated rounding rule. RC-003's 81-point probe used a corner-exact
formula and measured 0 px, but a grid that includes the corners does not stress
the rounding. Under either of two plausible rules —

- **A** `floor(abs·size / 65536)`
- **B** `round(abs·(size−1) / 65535)`

— the naive `abs = round(p·65535/(size−1))` is **off by a pixel** for widths of
1080 and up (mutation-confirmed: the build fails at 1080, 1366, 1920, 2560, 3840,
5760 and 7680; it passes below 800). Aiming at the **pixel centre**,
`abs = floor((2p+1)·32768/size)`, is exact for **every pixel** under both rules,
and rustc proves it for ten screen widths including 3840 and 7680 (two or three
displays side by side).

🔴 Limit of that proof: it shows the arithmetic is right against the two models,
**not that Windows uses either**. Which rule Windows really applies, and how it
behaves on a negative-origin virtual desktop with mixed DPI, is only answerable on
a real multi-monitor machine. So the adapter also reads the cursor back after each
absolute move and returns `pointer_error` if it is more than 2 px off (the
acceptance budget), rather than trusting the arithmetic.

### Behaviour of the adapter

- Pointer position and the button/wheel it belongs to go in **one** `SendInput`
  call, so nothing can land between "move here" and "click".
- Keys are scan codes with `wVk = 0`; no virtual-key integer from the network.
- Text is `KEYEVENTF_UNICODE` UTF-16 (an emoji is two pairs); LF and TAB become the
  Enter and Tab keys.
- Every event carries a tag in `dwExtraInfo` (the OS already marks injected input).
- `SendInput` access-denied (secure desktop, lock screen, UAC prompt) →
  `input_blocked` → the core ends the grant.

### The monitor is built not to be a key logger

A low-level hook is what key loggers are made of, so: injected events are ignored
outright; **no key value, character or position is stored** — the only per-key
state is a bitmask of whether each of the **eight modifier keys** is physically
held (needed so ending a session does not release a Shift the owner is pressing),
and every other key is just "activity happened"; mouse movement needs 8 px to
count; and the hook exists **only while a grant can inject** (`PhysicalMonitor::start`
at activation, drop at its end), never at idle. It is restartable per grant. The
hook procedure only touches atomics (a slow one is silently removed by Windows).
Some antivirus products flag any such hook.

### Not verified, and where the risk is

- **No test has been executed** — 54 platform-windows tests (47 unit, 7 through the
  real gate) and 151 core tests are compiled and linked only.
- The `SendInput`/`GetCursorPos`/hook/monitor calls have never run against a real
  OS. Mixed-DPI, negative origins, rotation and hot-plug are all unobserved.
- **UIPI is not detected.** Injecting into a higher-integrity window (an elevated
  app, a UAC prompt) is silently dropped by Windows while `SendInput` reports
  success. That needs a foreground-integrity check before it can be a clear
  failure message; today it would just appear not to work.
- **Wheel sign is an assumption** (DOM down = Windows negative) about a browser
  engine (RC-015) that does not exist yet.
- `ensure_per_monitor_v2` verifies rather than assumes, but nothing wires it into
  `app` yet.
- Nothing yet turns `PhysicalState::take_activity` into
  `TransportEvent::PhysicalActivity`: the app must poll it from the watchdog.
- `display_id` is the Windows device name; hot-plug can rename it, which is
  intended (a changed fingerprint ends the grant).

### Next

`capture.rs` (WGC → I420 → `NativeVideoSource`, `CreateFreeThreaded`, `new_black`,
`is_screencast: true`, simulcast decision) on a machine that can build the
`windows` and LiveKit crates; then RC-014 (the native consent dialog, which must
use `provenance::click_is_physical`, and the emergency hotkey).

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
