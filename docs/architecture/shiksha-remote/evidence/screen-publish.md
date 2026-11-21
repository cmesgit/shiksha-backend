# RC-003 — native screen publish into a live room

**Status: answered.** A Windows Graphics Capture frame reaches the SFU as a
1920x1080 VP8 screen-share track, confirmed from the server side rather than
from the publisher's own claims.

## What was measured

The spike captures the primary display with WGC, copies each frame to a D3D11
staging texture, converts BGRA to I420 with libyuv, and hands it to
`NativeVideoSource::capture_frame`. It publishes for 15 seconds, then leaves.

Publisher side, three consecutive runs:

```
capture  : 1920x1080
CONNECTED in 462 ms
PUBLISHED track sid: TR_VSxwDwLGqfKpeD
  frames captured : 466  (31.0/s)
  frames accepted : 448
  BGRA->I420      : p50 1.59 ms   p95 2.41 ms   max 4.75 ms
  I420 stride == width: every byte of the buffer is written
```

Server side, polled every 2 s through the same window with the management API
on the dev VM:

```
+  0.1s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+  2.2s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+  4.2s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+  6.3s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+  8.3s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+ 10.3s  rc003-spike state=2  [TR_VSxwDwLGqfKpeD 1920x1080 video/VP8 muted=False]
+ 12.4s  (empty)
```

The track sid printed by the publisher and the sid reported by the server are
the same string, `source=3` is SCREEN_SHARE, and the dimensions match the
display exactly. The disappearance at +12.4 s is the sampler outliving the
probe's 15 s window, not a mid-publish drop — an earlier single-sample check
caught only that tail and briefly looked like a dropped track, which is why
this was re-run as a continuous sample. One snapshot cannot distinguish "never
arrived" from "already finished".

## Four findings worth carrying into RC-012

### 1. The source silently caps frame rate at ~30 fps

`frames accepted` was **448 in all three runs** while `frames captured` varied
between 466 and 491. A constant output against a varying input is a cap, not a
coincidence: 448 over 15 s is 29.9 fps. `capture_frame` returns `false` for
frames the source's own rate adapter discards, and that return value is easy to
ignore because nothing fails when you do.

So capturing as fast as WGC will deliver is wasted work — roughly 4-9% of frames
here, and much more on a busy screen. The companion should pace capture to the
negotiated rate instead of free-running. On a teacher's laptop that is battery
and fan noise spent producing frames that are thrown away immediately.

### 2. Default simulcast gives screen content a useless low layer

The server reports LiveKit auto-created two layers:

```
layer q=0   960x540  bitrate=150000
layer q=1  1920x1080 bitrate=3000000
```

A 960x540 layer at 150 kbps is reasonable for a face. For a text-heavy screen it
is illegible — and illegible is not a degraded remote-control session, it is a
failed one. A controller who cannot read the screen cannot safely click on it.

`TrackPublishOptions` exposes `simulcast_layers` and `simulcast`. RC-012 should
decide deliberately between disabling simulcast for screen share and supplying
presets whose lowest layer stays readable, rather than inheriting a default
tuned for camera video.

### 3. `is_screencast: true` is a correctness flag, not a label

`NativeVideoSource::new(resolution, is_screencast)` sets WebRTC's degradation
preference. With it true, the encoder under constraint drops frame rate and
preserves resolution; with it false, it blurs to keep motion smooth. For remote
control the first is right and the second is harmful, for the same reason as
above: readable and choppy beats smooth and unreadable.

Nothing warns you if this is wrong. The session works either way, and the
failure only shows up on a constrained connection — at a pilot site, not here.

### 4. `I420Buffer::new` returns uninitialised memory

This is the SDK's own note, in the keepalive path of `NativeVideoSource`:

> This buffer reaches the encoder without any plane ever being written, so it
> must be black-initialized: `I420Buffer::new` leaves the pixel data
> uninitialized and would leak recycled heap memory to subscribers in the first
> keyframes.

We measured `stride == width` for all three planes at 1920x1080, so libyuv
writes every byte of our buffers and nothing uninitialised escapes. But the
guarantee is incidental, not structural: it holds because the strides happen to
equal the width at this resolution. Any buffer whose stride exceeds its width
has padding columns that libyuv does not touch, and those bytes would be encoded
and sent to whoever is watching.

For a product whose entire purpose is to show one person's screen to another,
leaking arbitrary heap memory into the video stream is a bad failure mode. RC-012
should allocate with `new_black`, or assert `stride == width`, rather than rely
on a coincidence of resolution. The spike prints which case it is on every run
for exactly this reason.

## Cost

BGRA to I420 runs about 1.5 ms at p50 and under 3 ms at p95 for 1920x1080,
single-threaded libyuv on the capture thread. Against a 33 ms frame budget that
is roughly 5%, comfortable. It scales with pixel count, so a 4K display costs
about four times as much — near 6 ms p50 — which is still affordable but is no
longer negligible, and is worth re-measuring before promising 4K support.

## What this does not cover

- **A subscriber now exists.** See `controller-subscribe.md`: a browser decodes
  the track at 1920x1080 and the image is upright, unmirrored and the right
  display, so the contract holds across encode and transport too.
- **One display, scale 1.00, hardware D3D.** Multi-monitor and fractional DPI
  remain untested and still need only a second display placed to the LEFT of
  primary.
- **An idle-ish desktop.** 31 fps captured reflects moderate screen activity.
  WGC delivers on change, so a static screen yields fewer frames and a video
  playing full-screen yields more.
- **Measurement 3 is still open.** Capture, conversion and injection are all
  small and now all measured. The browser and SFU hops, where the latency budget
  will actually be spent, remain unmeasured.

## Spike status

`rc003-livekit` is throwaway, like `rc003-spike`. Phase 0's rollback is "delete
isolated spike; keep documents/tests" — these findings are the deliverable, not
the code.
