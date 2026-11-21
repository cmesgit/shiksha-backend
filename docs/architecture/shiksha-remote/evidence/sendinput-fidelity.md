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