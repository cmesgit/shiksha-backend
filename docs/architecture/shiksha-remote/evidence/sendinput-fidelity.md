# RC-003 part 2 — SendInput coordinate fidelity, measured

Date: 21 November 2026. Windows 11 Home 26200, single 1920×1080 display,
no DPI scaling. Probe: `SendInput` with
`MOUSEEVENTF_MOVE | ABSOLUTE | VIRTUALDESK`, read back with `GetCursorPos`
in a DPI-aware process.

**This closes the injection half of RC-003's measurement 2.** It does not
close measurement 1 or 3; see "What this is not" below.

## Result

| | |
|---|---|
| Samples | 81 (9×9 grid over the virtual desktop, including all four edges) |
| Max error | **0 px** |
| Mean error | **0 px** |
| Within the ≤2 physical-pixel budget | **81/81** |

The mapping under test is the one `realtime-event-contracts.md` specifies:

    x = left + round(u × (width − 1))
    y = top  + round(v × (height − 1))

then normalised to `SendInput`'s 0..65535 absolute virtual-desktop space.
It is exact at every sampled point, corners included — `u=0,v=0` and
`u=1,v=1` both land dead on, which is where an off-by-one in the
`width − 1` term would show first.

Call latency, injection side only:

| | |
|---|---|
| p50 | 0.42 ms |
| p95 | 0.73 ms |
| max | 7.23 ms |

Against the 150 ms p95 event→inject budget, the `SendInput` call itself is
noise. Whatever spends that budget, it is not this.

## 🔴 The first run said 0/81 and was wrong

The first attempt reported **every** sample failing by hundreds of pixels,
with a mean error of 700 px. That looked like a damning result for the
architecture. It was a bug in the probe.

Every reading came back as *exactly* the cursor's starting position. That
is the tell: a genuinely inaccurate injection produces varying wrong
answers, not one constant. The cursor had not moved at all.

`SendInput` was returning **0** with `GetLastError() = 87`
(`ERROR_INVALID_PARAMETER`). The `INPUT` struct had been declared with two
extra `long` fields, making it 56 bytes; on x64 it must be exactly **40**
— `DWORD type`, 4 bytes of padding, and a 32-byte `MOUSEINPUT`, which is
the largest member of the union. The wrong `cb` made Windows reject every
call.

Two things were confirmed before blaming the environment: the process is on
window station `WinSta0`, desktop `Default`, and `SetCursorPos` moved the
cursor successfully from the same process. So desktop access was never the
problem.

The probe now **aborts loudly** if `SendInput` returns anything but 1. A
silent injection failure must never again be able to masquerade as a
measurement — it argues for exactly the wrong conclusion, and it argues for
it confidently.

## What this is NOT

* **Not measurement 1.** Native screen publish rendering in an existing
  LiveKit room is untested. That needs the Rust SDK; this machine has no
  Rust, no MSVC (`cl`), no cmake, no ninja and no Windows SDK.
* **Not the full measurement 2.** The WGC capture half is untested for the
  same reason. What is proven is that *given* a correct normalised
  coordinate, the injection lands on the right physical pixel.
* **Not measurement 3.** This is the `SendInput` call, not the
  event→inject path. The 150 ms p95 budget covers browser → SFU →
  companion → injection; only the last step is measured.
* **Not the hard cases.** One monitor, scale 1.0. The spec's difficult
  geometry is **multi-monitor** (negative origins — note the example
  descriptor's `"x": -1920`) and **fractional DPI** (`dpi_scale` 1.25,
  1.5). Neither is exercised here. A second display is the single most
  valuable thing to add.

## Consequence

The `width − 1` mapping and the virtual-desktop normalisation are correct
on the simple case, and the injection call is far too cheap to matter in
the latency budget. Neither was in serious doubt, but both were assumed
rather than measured, and RC-003 lists them as Stop criteria.

The remaining risk in RC-003 is concentrated where it always was: capture,
the SDK, and multi-monitor geometry. None of it can be reached without the
toolchain.
---

## Addendum, same day — the toolchain kill-criterion is closed

RC-003 could not be started at all because this machine had no Rust, no
MSVC, no cmake, no ninja and no Windows SDK. That is no longer true:

| | |
|---|---|
| Rust | 1.100.0, host `x86_64-pc-windows-msvc` |
| MSVC | 14.44.35207 (VS 2022 Build Tools 17.14.45) |
| Windows SDK | 10.0.22621.0 and 10.0.26100.0 |
| `windows` crate | 0.58.0, compiles and links |

The installer asks for a restart; command-line builds do not need one.

A throwaway spike at `C:\work\rc003-spike` (**not** the companion, and not
committed — the roadmap's Phase 0 rollback is "delete isolated spike; keep
documents/tests") builds and runs:

    per-monitor-v2 DPI awareness : YES
    virtual desktop              : origin=(0,0) size=1920x1080 monitors=1

    \.\DISPLAY1    1920x1080  at (0,0)  dpi=96  scale=1.00  [primary]
        u=0 v=0 -> physical (0,0)       -> SendInput abs (0,0)
        u=1 v=1 -> physical (1919,1079) -> SendInput abs (65535,65535)

Both corners map exactly, which is where an off-by-one in the `width − 1`
term would appear first — this is the same arithmetic the PowerShell probe
exercised, now reproduced in the language the companion will be written in.

🔴 It declares `PER_MONITOR_AWARE_V2` **before** reading anything and prints
whether that succeeded. Without it Windows returns virtualised coordinates
and every number would be a confident, plausible lie — the same failure
shape as the 0/81 above. The spike also prints an explicit warning when it
sees one monitor or all-96-DPI displays, so a green run here can never be
mistaken for having covered the hard cases.

### What is now genuinely left in RC-003

1. **Native screen publish into an existing LiveKit room.** Needs the
   LiveKit Rust SDK and real credentials. Untouched.
2. **WGC capture, and the capture→desktop mapping.** The injection half is
   measured; the capture half is not.
3. **Multi-monitor and fractional DPI.** Still unobservable here: one
   display, scale 1.00. The spec's example descriptor carries `"x": -1920`,
   a monitor to the LEFT of primary, and that negative origin is exactly
   what breaks code assuming the desktop starts at (0,0). **Attaching a
   second display is the cheapest way to close this**, and it needs no
   further software.

The toolchain was the gate. It is open; the measurements behind it are not
done.
---

## Addendum 2 — WGC capture measured; measurement 2 is now closed

Same machine, same session. The capture half now runs, so RC-003's
measurement 2 is complete for the simple case:

    d3d driver   : hardware   frames: 5   first frame: 16.6 ms
    item size    : 1920x1080
    frame size   : 1920x1080
    monitor size : 1920x1080   -> capture matches display: YES

**The captured frame is exactly the monitor's physical pixel size.** That
is the claim the injection mapping rests on. Had it differed by any factor,
`x = left + round(u × (width − 1))` would be wrong by that proportion —
near-perfect in the centre of the screen and worst at the edges, which is
the signature that gets misdiagnosed for weeks as "the remote mouse feels
slightly off" rather than as a scale error.

Five consecutive frames were taken rather than one, so the size is stable
rather than correct once by luck on the first buffer.

**First frame arrived in 16.6 ms** — about one refresh at 60 Hz. That is
the capture-side contribution to the 150 ms p95 event→inject budget, and it
is small. It is *not* the full path; see below.

The D3D device came up on **hardware**, not WARP. Worth recording: a spike
that silently measured the software rasteriser would say nothing useful
about what a teacher's laptop does.

### Two implementation notes for RC-012/RC-013

🔴 **`Direct3D11CaptureFramePool::CreateFreeThreaded`, not `Create`.** The
ordinary `Create` requires a `DispatcherQueue` on the calling thread. A
console or service process has none, and the failure is an opaque
`RPC_E_WRONG_THREAD` that names nothing about the real cause.

**Windows draws its own capture border** around a display under WGC. The
permission model wants the companion to be *observable* — "a visible
associated LiveKit participant", so an unexpected second companion is
noticeable rather than silent. The OS border is a second, stronger version
of that same guarantee, enforced below our code and not removable by a
modified companion. Worth stating in the UX spec as a feature rather than
discovering it as a surprise.

### RC-003 status after this

| Measurement | State |
|---|---|
| 1 — native screen publish into a live LiveKit room | **not started** (needs the LiveKit Rust SDK and real credentials) |
| 2 — WGC ↔ SendInput coordinate fidelity ≤2 px | **closed** for one display at scale 1.00 — both halves exact |
| 3 — p95 event→inject < 150 ms | **partial**: capture 16.6 ms, injection 0.73 ms p95. The browser→SFU→companion hops are unmeasured and are where the budget will actually go |

Still unobserved, and still needing no software: **multi-monitor and
fractional DPI**. One display at scale 1.00 is the easy case. The spec's
own example descriptor carries `"x": -1920`, a display to the LEFT of
primary, and that negative origin is exactly what breaks code assuming the
desktop starts at (0,0).
