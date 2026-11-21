# RC-003 — the controller side: subscribe, decode, and the control round trip

**Status: partly answered, and the unanswered part is blocked on method, not
on code.** A browser subscriber decodes the companion's screen correctly, and
the control-message round trip is measured. Screen-to-glass latency is **not**
measured, for a reason worth recording rather than papering over.

## What is now proven

### The frames survive the whole path, right way up

A browser joined the room, subscribed to the companion's track and decoded it
at **1920x1080**, up to ~2200 frames in a 75 s window. Rendering the received
video back on screen shows the desktop upright, unmirrored, and the correct
display: the conversation pane on the left and the dark controller panel on the
right, matching the physical layout, with text reading left to right.

That matters because `realtime-event-contracts.md` asserts the native capture
adapter "supplies an upright, unmirrored image of the selected display", and
until now that was verified only at the point of capture. A vertical flip is a
classic D3D/WebRTC seam bug — the two conventions disagree about which row is
row zero — and it would have made every injected click wrong in y while looking
perfectly plausible in a static screenshot. It does not happen here.

### The control round trip is about 150-180 ms

The companion echoes any data packet on topic `ping` straight back on `pong`,
unreliable, so the measurement follows the path a real input event takes:
controller to SFU to companion, and back. Measured from one clock, which
sidesteps clock skew entirely.

Six runs, from a residential connection in India to the dev LiveKit Cloud
project:

| samples | p50 | p95 |
|--------:|----:|----:|
| 21  | 161.3 | 165.1 |
| 29  | 158.4 | 163.0 |
| 47  | 170.7 | 179.7 |
| 76  | 147.6 | 152.4 |
| 172 | 148.9 | 153.5 |
| 237 | 162.3 | 178.7 |

So a **round trip of roughly 150-180 ms, and a one-way control hop near
75-90 ms** if the path is symmetric — which was not measured and should not be
assumed, since routing often is not.

Put beside the numbers already in hand, the shape of the budget is now clear
and it is not what a reader of the earlier evidence might expect:

| stage | cost |
|---|---|
| `SendInput` injection | 0.73 ms p95 |
| BGRA to I420 conversion | 1.5 ms p50 |
| WGC first frame | 16.6 ms |
| **control hop, one way** | **~75-90 ms** |

The three local stages together are under 20 ms. The network dominates by a
factor of four or more. Any further optimisation of capture or injection is
noise against the transport, and RC-012 should not spend effort there.

## What is not measured, and why

**Screen-to-glass latency — the time from a pixel changing on the teacher's
screen to that change appearing for the controller — is still unmeasured.**

The method works and is built: toggle a large area between black and white at
randomised intervals, publish the desktop, subscribe to it, sample each decoded
frame's mean luminance, and pair the detected transition with the toggle that
caused it. Randomised intervals matter, because a fixed cadence can alias
against the capture and encode rates and yield a stable, confident, wrong
number. Both halves run in the same page, so there is one clock and no skew.

It cannot be completed unattended, because of a genuine conflict:

- The blinking surface must be **visible and in the foreground on the very
  display being captured** — that is the whole point.
- The subscriber must render frames promptly, and a browser **throttles
  `requestVideoFrameCallback` in a background or covered window**, to a fraction
  of a frame per second.
- An agent driving the machine keeps taking the foreground, so the window under
  measurement is repeatedly backgrounded.

Observed directly: with the window covered, 17 frames arrived in 80 s and the
captured luminance range was 1.7 out of 255 — the blink never reached the
capture at all. With it in front, 2218 frames arrived and the range was 235.
Same code, same room, two orders of magnitude apart. A latency figure taken
from the first case would have been meaningless and entirely plausible-looking.

Two honest ways to finish it:

1. **A human-attended run**, with the controller window in front and untouched
   for the duration. The page already prints p50/p95/min/max.
2. **Two machines** — publish from one, subscribe on the other — which also
   removes the hall-of-mirrors effect of a screen displaying itself. This costs
   clock synchronisation: NTP alone is not tight enough for a ~100 ms quantity,
   so prefer a round-trip-based estimate over one-way wall-clock subtraction.

## A measurement bug worth remembering

The first version paired each detection against the **oldest** queued toggle.
That looks careful and is wrong: one missed transition shifts every later pair
by a whole toggle interval. It reported p50 1049 ms against a minimum of
151.3 ms — a spread that is bookkeeping, not latency, and the minimum was the
only honest number in it.

Two things made it detectable rather than publishable. The minimum sat
suspiciously close to the independently measured data round trip. And a
re-run produced a maximum of **2855 ms, identical to the previous run's
maximum to the decimal** — which is impossible for independent samples, and
revealed that a hash-only URL change had not reloaded the page at all, so the
fix under test had never run.

The corrected rule keeps only the most recent toggle into each state. Because
states alternate, a given state recurs only after two toggles (>1 s), so any
latency well under that pairs unambiguously.

## Reproducing

`rc003-livekit` contains `controller.html` (the probe page) and `src/bin/
serve.rs`, a ~60-line dependency-free static server bound to `127.0.0.1:8099`.
The server exists because the in-app preview pane renders `file://` URLs as
snapshots with no network, and Chrome refused the `file://` URL outright;
localhost is a secure context, so serving the same bytes over HTTP satisfies
WebRTC without adding a dependency.

Tokens are minted on the dev VM, scoped to one room, with short TTLs, and
written to a generated `probe-config.js` that is deleted after the run rather
than passed on a command line or in a URL. The API secret never leaves the VM.
The controller token is `canSubscribe` only — it has no media of its own and
should not be able to invent any.

## Spike status

Throwaway, like the other RC-003 spikes. Phase 0's rollback is "delete isolated
spike; keep documents/tests".
