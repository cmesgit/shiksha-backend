# REMOTE — Shiksha Remote handoff

Everything another chat needs to continue the remote-control feature for
shikshacom.com. Written 21 November 2026.

Read [CLAUDE.md](CLAUDE.md) first for workspace facts (SSH, repo layout,
credentials, gotchas). This file is the project.

---

## 1. What is being built

A teacher or peer can **see and control another participant's desktop** from
inside an existing live session. Four parts:

| part | where | state |
|---|---|---|
| Django control plane — identity, consent, grants, leases | `shiksha-backend` | **done** |
| Browser UI — setup, consent dialog, status bar | `shared/` → 3 web apps | **done** |
| **Native companion — the app that captures and injects** | `shiksha-remote-companion/` | **core written (RC-012 part 1); tests not yet run** |
| Browser input engine — controller sends mouse/keys | not started | not started |

The companion is the thing that actually does the controlling. **Nothing
works without it.** The Rust in `rc003-spike/` and `rc003-livekit/` is
throwaway measurement code, not a starting point for it — Phase 0's rollback
is explicitly "delete isolated spike; keep documents/tests".

### The shape that matters

Two independent gates, and conflating them is the mistake to avoid:

1. **The server** decides who *may* ask and records consent.
2. **The companion** independently decides whether to *inject*, and only
   with a local physical confirmation on the controlled machine.

"No write to frontend state can open native gate." A browser can never start
control on its own, and `approved` is a deliberately separate state from
`active` for exactly this reason.

---

## 2. The spec pack is the source of truth

`shiksha-backend/docs/architecture/shiksha-remote/`

Reading order for a new contributor:

1. `README.md`
2. `task-dependency-graph.md` — the 31 tasks and what blocks what
3. `architecture-decisions.md` — ADRs **and** the open decisions (OD-01…)
4. `tasks/phase-N/RC-0NN.md` — the task you are doing
5. `evidence/` — what each completed task actually proved

Then the contracts, which no task may fork:
`api-contracts.md`, `realtime-event-contracts.md`,
`remote-control-protocol.md`, `permission-security-model.md`,
`frontend-ux-specification.md`, `security-test-plan.md` (T01–T24).

**Docs branches:** `docs/shiksha-remote-plan-on-dev` and
`docs/shiksha-remote-plan-on-main`. Evidence commits go to the `-on-dev` one.

---

## 3. Where the work stands

**RC-001 … RC-011 are done and merged.** That is 11 of 31 tasks: all of
Phase 0, 1 and 2.

Evidence in `evidence/`: `runtime-baseline.md`, `contract-freeze.md`,
`collaboration-schema.md`, `participant-bindings.md`, `device-pairing.md`,
`consent-state-machine.md`, `activation-and-leases.md`,
`control-grant-routes.md`, `legacy-retirement.md`, `host-bindings.md`,
`companion-presence.md`, `collaboration-socket.md`, `sendinput-fidelity.md`,
`livekit-sdk-windows.md`, `screen-publish.md`, `controller-subscribe.md`,
and others.

### RC-003's measured budget — the headline result

| stage | cost |
|---|---|
| `SendInput` injection | 0.73 ms p95, **0 px error over 81 samples** |
| BGRA→I420 conversion | 1.5 ms p50 |
| WGC first frame | 16.6 ms |
| control hop, one way | ~75 ms |
| **screen to glass** | **185 ms p50, 234 ms p95** |

**The loop a controller feels is ~260 ms plus the remote app's redraw, and it
is dominated entirely by transport.** Everything we write totals under 20 ms.
Do not spend effort optimising capture or injection — the levers are the
encoder and the network.

Design consequence: 260 ms is fine for pointing, clicking and filling forms.
It is **not** fine for dragging or freehand drawing. Prefer click-to-place
interactions.

### RC-012 part 1 — 26 Nov 2026, second machine

`shiksha-remote-companion/` now holds three crates. **Read
`evidence/companion-core.md` before trusting any of it** — the short version:

- `core` (std-only authorisation: gate, latch, leases, sequencing, executor,
  fake adapter) passes `cargo check --all-targets` and its 183 tests **compile
  and link but have never been executed**.
- `transport` (JSON decode vs the shared fixtures, Ed25519 JWT, entropy) and
  `app` are **written but have never been compiled**.
- Cause: this Windows machine runs Application Control that blocks unsigned
  executables (os error 4551), which stops test binaries *and* build scripts, so
  only dependency-free crates can be checked. Not worked around.
- LiveKit join and status wiring is **written but never compiled**
  (`crates/livekit`, `transport::session`; see evidence file Part 2).
- The DPoP client, credential refresh state machine and typed device API are
  written (evidence file Part 3): pure parts in `core` are checked, the HTTP/key
  parts in `transport` are not.
- A DPAPI-backed secret store exists (`crates/platform-windows`, evidence Part 4;
  type-checked, never run). 🔴 DPAPI does not stop other code running as the same
  user; a TPM-held non-exportable key would — decide before the pilot.
- Driver decision logic is in `core::drivers` (checked); the blocking workers
  (`transport::runtime`) and a CLI shell (`app`: `--pair`, refresh + heartbeat)
  are written but never compiled (evidence Part 5).
- **RC-013 part 1** (evidence Part 6): `crates/platform-windows` now has the
  `SendInput` adapter, key map, display geometry and a physical-input monitor,
  all **type-checked**, with 44 compile-time assertions that rustc actually
  evaluated (key coverage, `INPUT` = 40 bytes, and a proof that the pixel-centre
  absolute-coordinate formula is exact for every pixel under two rounding models;
  the "obvious" formula is not). **Capture (WGC) is not started.**
- **RC-014 part 1** (evidence Part 7): the consent dialog as a tested state
  machine (`core::consent`: deny-by-default, dwell, physical-only approval,
  deny-on-timeout), no activation without a registered physical Stop, the
  Ctrl+Alt+Shift+F12 hotkey, and a process-kill cleanup helper with a pipe-death
  signal. **No native window is written.** **T09 has not been run, so held-input
  release is NOT certified.** The dwell length is a placeholder that needs a
  safeguarding owner's decision.
- Not built: the share/control flows and LiveKit composition. They need RC-013
  (capture/input adapter) and RC-014 (native consent UI). `--pair` confirms on the
  console, which is a dev stand-in only. The Windows adapter and native UI are RC-013/014.

Still open in RC-003: **multi-monitor and fractional DPI**. Needs no code —
only a second display placed to the **LEFT** of primary so the virtual-desktop
origin goes negative.

### Decisions that constrain everything downstream

- **ADR-02 Option C.** The companion's LiveKit token must grant
  `can_subscribe: true` — LiveKit has no data/media split. Media isolation
  moves client-side and authority moves into the signed payload.
- **OD-01, RESOLVED 11 Nov 2026, split by context.** In an **instant
  collab**, anyone the host admitted may request control — including an
  identity-free guest. In **classrooms**, teacher-initiated only. Several
  documents still say "OD-01 blocks RC-011" in retained historical framing,
  and one line in `adr-02-revision-proposal.md` is simply stale.
- **OD-02, RESOLVED.** Audit records retained **90 days**, then auto-deleted.
  Ninety because safeguarding complaints arrive late; not indefinite because
  these are records about minors.

---

## 4. What exists in code

### Backend — `shiksha-backend/sessions_app/collaboration/`

| file | owns |
|---|---|
| `grants.py` | the nine-state consent machine; every write to a v1 grant |
| `leases.py` | Ed25519 activation tickets and ≤3 s leases |
| `host_bindings.py` | Redis capture-binding lifecycle |
| `companion_tokens.py` | `rcdev_` prefix, join tokens, ADR-02 Option C |
| `grant_views.py`, `host_binding_views.py` | route surfaces |

Load-bearing details that are easy to break:

- **Lock the device row before the grant row, always.** `_lock_grant` peeks
  `device_id` unlocked, takes the device, then the grant.
- **`_sweeps_expiry` runs the expiry sweep in its own committed transaction
  before the decorated atomic block opens.** Without it a caller that finds an
  elapsed grant refuses, the rollback discards the expiry write, and the stale
  row holds the device slot forever. The partial index has no `NOW()`.
- **`_assert_live` reports 410/409 before `_check_version`**, or the sweep's
  version bump makes an expired grant look like a version conflict.
- **`assert_may_request_control` restates OD-01 directly** and must never read
  `group_session_policy.remote_control_enabled` — legacy, inverted meaning.

### Shared frontend — `shared/src/`

`api/collaborationService.js`, `hooks/useCollaboration.js` (RC-010), and
`components/live/assistance/` (RC-011): `AssistanceProvider`,
`AssistanceMenu`, `CompanionSetupDialog`, `ControlRequestDialog`,
`ControlStatusBar`, `assistance.css`.

43 tests live in `shared/` and run against the **canonical** source, not the
three synced copies. `sync.mjs` guarantees those are byte-identical, and the
apps have no component-test dependencies at all.

`ControlRequestDialog.jsx` carries **T22** and should not be edited without a
safeguarding reviewer: Allow opens disabled and unfocused, Deny takes initial
focus, a dwell timer gates approval, Escape denies, the copy names the real
capability, and there is no auto-approving countdown or trust-forever option.

---

## 5. Do this next: finish RC-012

**Part 1 is in** (see §3 and `evidence/companion-core.md`). First action on a
machine that can execute binaries: `cd shiksha-remote-companion && cargo test
--workspace`, fix `transport` compile errors, then mutation-check the core
(suggested breaks are listed in the evidence file). Then the LiveKit join and
DPoP/pairing client. Everything below still applies to that remaining work.

`tasks/phase-3/RC-012.md` — *Implement native authenticated core and
transport*. Then RC-013 (capture/input adapter), RC-014 (native consent and
emergency cleanup), RC-015 (browser input engine), RC-016 (end-to-end
validation). Those five are the critical path to a session you can actually
use.

**The research is already done.** RC-012 is construction against known
numbers, not exploration. What the spikes proved, with the exact traps:

- **The LiveKit Rust SDK builds, links and joins a room on Windows.** It needs
  `-C target-feature=+crt-static` (the prebuilt WebRTC is `/MT`; the error
  names `MT_StaticRelease` first and misleads you into the opposite fix, which
  fails byte-identically). Consequence: the companion links the CRT statically
  and needs **no MSVC redistributable**.
- **TLS is not enabled by default.** `livekit` 0.12 has `default = ["native"]`
  with no TLS; it compiles and then fails at connect time with a message that
  never mentions Cargo features. Use `rustls-tls-native-roots` — it reads the
  OS trust store, which matters because schools run TLS-inspecting proxies
  whose roots are installed system-wide.
- **Capture is pixel-exact** and `CreateFreeThreaded` is required (plain
  `Create` wants a `DispatcherQueue`; a service process has none and the
  failure is an opaque `RPC_E_WRONG_THREAD`).
- **`SendInput`'s `INPUT` struct is 40 bytes on x64.** A 56-byte struct
  returns 0 with `lastError=87` and *looks* like an environment problem.
- **`is_screencast: true`** sets maintain-resolution degradation. Wrong value
  = blurred text only on constrained connections, i.e. only at a pilot site.
- **Default simulcast gives screen content a useless 960×540 @150 kbps
  layer.** Illegible text is a failed control session, not a degraded one.
- **`I420Buffer::new` returns uninitialised memory.** Use `new_black` or
  assert `stride == width`, or padding bytes leak heap into the video stream.

Read `evidence/livekit-sdk-windows.md`, `screen-publish.md`,
`controller-subscribe.md` and `sendinput-fidelity.md` before writing companion
code. They exist so this is not rediscovered.

---

## 6. How to operate

**There is no Node on the Windows machine.** Everything JS runs on the dev VM
(Node 20). `python` is the Microsoft Store stub and errors.

```bash
ssh shiksha-dev                      # User is root; see CLAUDE.md
```

| task | how |
|---|---|
| run shared frontend tests | VM: `cd shared && npm install && npm test` |
| sync shared → an app | VM, from the app dir: `node ../shared/sync.mjs` |
| verify parity | `node ../shared/sync.mjs --check` |
| build an app | VM: `npm run build` |
| push | from `/tmp/sharedpush/<repo>` clones on the VM, as `appuser` |
| deploy to dev | VM: the deploy script; pulls **`Dev`**, backs up `dist`, builds, restores on failure |

⚠ **The dev server deploys from `Dev`; PRs have been merging to `main`.** They
have diverged both ways. RC-011 only reached dev after merging `main` **into**
`Dev` in all three apps (one conflict each in `collaborationService.js` —
main's copy is canonical). Decide deliberately which branch PRs should target.

**Never copy a shared file past `sync.mjs` with `cp`.** That is how a
122-line change became 1,268.

---

## 7. Release state

**Nothing is user-visible and nothing is on production.**

The feature is gated twice: `VITE_ASSISTANCE_ENABLED` (absent by default) and
the server feature flags (off, so every collaboration route answers 404 and
`available` stays false). Both halves must be on before any surface renders.
Dev has the code deployed; production has never been touched by this work.

### Before a human uses this

- RC-012 … RC-016 — the companion and the input engine
- **RC-028** — a **signed** companion installer. A Windows code-signing
  certificate has days-to-weeks lead time and can be started now; without it
  every teacher meets a SmartScreen warning while installing software that
  asks to control their computer.
- RC-029 safety/load/compatibility review, RC-030 staged rollout
- **T22's scripted peer-pressure walkthrough, judged by a safeguarding
  owner.** The 43 passing tests are evidence about the dialog and explicitly
  **not** evidence that consent was freely given.

Not on the path for a basic Windows group session: file transfer
(RC-017–019, 031), private/classroom/skill adapters (RC-020–022), macOS,
Linux and Flutter (RC-023–027).

---

## 8. How to work on this

Habits that caught real bugs here, each earned:

- **Mutation-test anything load-bearing.** Delete the guard and expect red. A
  consent test that stripped `disabled` off a button and clicked it passed
  whether or not the guard existed, because React decides `onClick` from the
  element's *props*. A test that cannot fail is worse than none — it counts as
  coverage.
- **Write positive tests, not only refusal tests.** A dead lease path was
  caught by four *success* tests; every refusal test passed happily.
- **Silent failures argue confidently for the wrong conclusion.** 0/81
  injection, a luminance range of 1.7/255, a build that stays green because
  nothing imports the broken module.
- **When a measurement says the world is static and the code says it is
  changing, photograph the world.**
- **Check `git show --stat` after every commit from Windows.** If the number
  is near twice the file length, it is CRLF.
- Commit messages explain **why**, including what was tried and rejected.
  Match the style on the docs branch.
- Report "not run" and "failed" plainly; never infer a pass.
