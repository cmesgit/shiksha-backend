# RC-003 — the controller side: subscribe, decode, and the latency budget

**Status: answered.** A browser subscriber decodes the companion's screen
correctly, the control round trip is measured, and screen-to-glass latency is
measured. With injection and capture already measured, RC-003's measurement 3
now has a complete shape.

## The budget

| stage | cost | source |
|---|---|---|
| `SendInput` injection | 0.73 ms p95 | `sendinput-fidelity.md` |
| BGRA to I420 conversion | 1.5 ms p50 | `screen-publish.md` |
| WGC first frame | 16.6 ms | `sendinput-fidelity.md` |
| control hop, one way | ~75 ms | below |
| **screen to glass** | **185 ms p50, 234 ms p95** | below |

A controller clicks; the event crosses to the companion (~75 ms); injection is
free (0.73 ms); the application redraws (its own cost, not ours); the new pixels
come back (185 ms p50). **So the controller waits roughly 260 ms plus the
application's own redraw before seeing the result of their own click.**

That is the number RC-012 has to design around, and it is dominated entirely by
things outside our code. Everything we write — capture, conversion, injection —
totals under 20 ms. Optimising any of it is noise. If the loop needs to feel
faster, the lever is transport and encoder settings, not our pipeline.

260 ms is usable for the pointing, clicking and form-filling a teacher actually
does. It is not usable for dragging, freehand drawing, or anything where the
hand expects the cursor to track it continuously. RC-012 should prefer
click-to-place interactions over drag interactions wherever the UX allows.

## Screen to glass, measured

Method: toggle a large area between black and white at randomised intervals and
record when; the companion captures the desktop and publishes it; the same page
subscribes, samples each decoded frame's mean luminance, and pairs the detected
transition with the toggle that caused it. Both halves live in one page, so
there is one clock and no skew to correct — the hard part of any one-way
latency measurement, sidestepped rather than solved.

Randomised intervals are not decoration. A fixed cadence can alias against the
capture and encode rates and produce a stable, confident, wrong number.

Two attended runs:

| samples | p50 | p95 | min | max | frames | luminance range |
|--------:|----:|----:|----:|----:|-------:|----------------:|
| 101 | 186.1 | 234.1 | 118.0 | 335.2 | 1838 | 236 |
|  36 | 184.3 | 234.2 | 151.6 | 270.7 | 1398 | 32 |

p50 agrees to within 2 ms and p95 to within 0.1 ms across runs with different
sample counts and very different contrast. That agreement is the reason to
believe the figure.

Caveats, stated because they bound the claim:

- Samples above 500 ms are discarded as unpairable at this toggle cadence, so
  **max is a floor on the worst case, not a measurement of it**.
- Both runs are one machine, one residential connection in India, one LiveKit
  Cloud region, publisher and subscriber on the same host. A real session puts a
  second network between controller and SFU; expect worse.
- This excludes the physical display: WGC captures the composed desktop, so
  panel latency is correctly outside the number, but so is the teacher's own
  monitor on the other end.

## The control round trip is 150-180 ms

The companion echoes any data packet on topic `ping` straight back on `pong`,
unreliable, so the measurement follows the path a real input event takes.
Unreliable deliberately: a mouse move that arrives late is worse than one that
never arrives, and retransmitting a stale position is exactly wrong.

Eight runs: p50 between 146.6 and 170.7 ms, p95 between 152.4 and 179.7 ms,
with sample counts from 21 to 287. So a **one-way control hop near 75-90 ms**
if the path is symmetric — not measured, and not safe to assume, since routing
often is not.

## The frames survive the whole path, right way up

The browser decodes at **1920x1080**, ~24 fps sustained. Rendering the received
video back on screen shows the desktop upright, unmirrored, and the correct
display: conversation pane left, controller panel right, matching the physical
layout, text reading left to right.

`realtime-event-contracts.md` asserts the capture adapter "supplies an upright,
unmirrored image of the selected display", and until now that was verified only
at the point of capture. A vertical flip is the classic D3D/WebRTC seam bug —
the two conventions disagree about which row is row zero — and it would have
made every injected click wrong in y while looking entirely plausible in a
static screenshot. It does not happen.

## Two measurement bugs, recorded because of how they were caught

**Pairing against the oldest queued toggle.** It looks careful and is wrong: one
missed transition shifts every later pair by a whole toggle interval. It
reported p50 1049 ms against a minimum of 151.3 ms. What exposed it was not the
silly median but the *minimum* sitting suspiciously close to the independently
measured data round trip — and then a re-run producing a maximum of 2855 ms,
**identical to the previous run's maximum to the decimal**, which is impossible
for independent samples and revealed that a hash-only URL change had never
reloaded the page, so the fix under test had never run. The corrected rule keeps
only the most recent toggle into each state; since states alternate, a given
state recurs only after two toggles (>1 s), so any latency well under that pairs
unambiguously.

**Measuring a tab that was not on screen.** Every attempt for an hour returned a
captured luminance range of 1.7-3.2 out of 255 and zero usable samples, while
the page itself reported `document.hasFocus() === true` and happily decoded
video. The blink was real and the capture was real; they were simply not the
same surface. An editor hook re-opened the source file in the preview pane on
every edit and fronted it, so the measuring page sat in a *background* tab while
a stale `file://` copy — the one showing "opened without web access" — was what
the screen actually displayed, and therefore what got captured.

No amount of reasoning from inside the page could have found that, because every
in-page signal was correct. It took one screenshot of the whole desktop. The
general lesson is worth more than the specific bug: **when a measurement says
the world is static and the code says it is changing, photograph the world.**

## Reproducing

`rc003-livekit` holds `controller.html` and `src/bin/serve.rs`, a ~60-line
dependency-free static server on `127.0.0.1:8099`. The server exists because the
preview pane renders `file://` URLs as snapshots with no network and Chrome
refused the `file://` URL outright; localhost is a secure context, so serving the
same bytes over HTTP satisfies WebRTC without a dependency.

The run must be attended: the blinking surface has to be visible and foreground
on the display being captured, and browsers throttle `requestVideoFrameCallback`
in a covered window to a fraction of a frame per second. Confirm the capture is
real before trusting any number — the page prints the luminance range, and a
range under ~20 means the blink never reached the capture.

Tokens are minted on the dev VM, scoped to one room, short TTL, written to a
generated `probe-config.js` that is deleted after the run rather than passed on a
command line or in a URL. The API secret never leaves the VM. The controller
token is `canSubscribe` only — it has no media of its own and should not be able
to invent any.

## Spike status

Throwaway, like the other RC-003 spikes. Phase 0's rollback is "delete isolated
spike; keep documents/tests".
