# E. Realtime event contracts v1

Single authoritative wire contract. RC-002 generates strict JSON Schema/fixtures and validators from this document before producers/consumers land. Unknown version/kind, additional properties, malformed IDs, nonfinite numbers, duplicate keys or oversized payloads are rejected. UTF-8 only. UUIDs lowercase strings. JS sequence integers 1..2^53−1; before overflow stop and require new epoch. No credentials/keystrokes in logs.

## LiveKit transport selection

Use **existing** `localParticipant.publishData` with explicit one-element `destinationIdentities`. Missing/empty destination is an error in the collaboration wrapper, never a room broadcast. Native receives SDK-authenticated sender identity and resolves current participant SID; payload identity alone is not proof. Ignore server-origin data (no participant) for input. Validate room, actual SID and immutable grant principals against local approved state. A public grant UUID is only a reference, not a bearer permission.

| Topic | Delivery | Messages | Ordering/limits |
|---|---|---|---|
| `shiksha.rc.motion.v1` | lossy | move | ≤60/s, ≤1200 bytes incl JSON; coalesce unsent motion |
| `shiksha.rc.input.v1` | reliable | button, key, text, wheel, reset, heartbeat | ≤120/s total, burst 240; ≤4096 bytes; one ordered action sequence |
| `shiksha.rc.status.v1` | reliable | ready, challenge, ack, stopped, error | ≤20/s; native→bound controller; ≤4096 bytes |
| `shiksha.file.receipt.v1` | reliable | received, failed, cancelled | one bound peer; also persist lifecycle via REST |
| `shiksha.file.bytes.v1` | byte stream | selected file bytes only | accepted transfer ID and exact receiver identity required |

LiveKit reliable delivery is best effort, not durable delivery after disconnect. Packet recommendations are documented in [S1](sources.md). Application ceilings above are stricter. RPC is useful for optional diagnostics, but has per-call timeout/overhead; never use RPC as consent authority or per-pointer transport. New upstream data tracks are unnecessary for the pinned MVP, especially their subscription semantics. Do not upgrade SDK just to adopt them.

## Controller input envelope

```json
{
  "v": 1,
  "grant_id": "00000000-0000-4000-8000-000000000001",
  "epoch": "00000000-0000-4000-8000-000000000002",
  "seq": 12,
  "challenge": "base64url-128-bit-native-nonce",
  "capture_generation": 3,
  "kind": "button",
  "body": {"button":"left","down":true,"u":0.25,"v":0.75,"motion_seq":37}
}
```

Envelope fields all required; no sender user ID/token needed. `seq` is independent per motion or input lane. For reliable lane require next seq; duplicate≤last_applied is acknowledged without reinjection. Gap: gate pause/release, report `sequence_gap`; do not apply later actions. Retransmission of discrete events by application is forbidden. A missing ack causes pause/release rather than blind replay. Native sends cumulative ack at least every 100ms while input flows; ack timeout 500ms pauses controller.

Lossy move seq must increase; discard older/duplicate. Coordinates normalized against visible **capture content**, finite [0,1]. Button/wheel/key events that depend on pointer location contain `u,v,motion_seq` and apply that position atomically before the event. Maintain a motion floor equal to the highest move seq referenced/applied by a reliable action; later-arriving lossy packets ≤floor cannot move cursor backward. Across lanes there is no transport order guarantee.

| kind | body (exact fields) | Meaning |
|---|---|---|
| move | `{u:number,v:number}` | absolute capture location; separate motion seq |
| button | `{button:'left'|'middle'|'right',down:boolean,u:number,v:number,motion_seq:int}` | no extra click message; double click is two ordered down/up pairs |
| wheel | `{dx:number,dy:number,u:number,v:number,motion_seq:int}` | normalized logical wheel steps, each delta −10..10; receiver adapter scales to native units; coalesce ≤16ms |
| key | `{code:string,down:boolean}` | physical DOM KeyboardEvent.code from allowlist; no OS virtual-key integers from network |
| text | `{text:string}` | ≤256 Unicode scalar values/1024 UTF-8 bytes; no NUL/control characters except LF/TAB; requires keyboard.text capability |
| reset | `{reason:'blur'|'visibility'|'pointer_cancel'|'user_pause'}` | release all remotely owned states; closes gate into pause |
| heartbeat | `{keys:string[],buttons:string[],last_ack:int}` | ≤16 keys, ≤3 buttons; compare state, release missing states; **never synthesize new key-down from heartbeat** |

Capabilities: `pointer.move`, `pointer.button`, `pointer.scroll`, `keyboard.physical`, `keyboard.text`; default request explicitly lists all desired capabilities, host can reduce. There is no `shell`, `filesystem`, `clipboard`, `elevate`, `secure_attention` or `unattended` capability. Keyboard protocol uses printable letters/digits, punctuation, arrows/navigation, F1..F12 and modifiers; finite DOM-code list below is materialized into fixtures in RC-002. Reject unsupported platform codes before native injection. Emergency-stop chord, Ctrl+Alt+Delete, Win+L and platform secure attention/lock shortcuts are never injectable. Browser-reserved keys may not reach JS; provide explicit permitted shortcut buttons (e.g. Ctrl+S, Ctrl+C/V within remote OS) instead of claiming browser capture of everything.

Key repeat is generated by OS from held physical key; ignore browser `event.repeat`. Text mode (compositionend/explicit text entry) and physical printable-key mode are exclusive to prevent double typing. No clipboard read/write synchronization. Map host layout, not controller assumptions; layout identifier is shown and tested. Controller can type into a remote terminal as part of ordinary desktop permission; no claim of application isolation.

Native status envelope: `{v:1,grant_id,epoch,kind,body}`; kinds `ready {capture_generation,track_sid}`, `challenge {nonce,valid_for_ms:1000}`, `ack {input_seq,motion_seq}`, `stopped {reason}`, `error {code}`. Challenge nonce random 128 bits, native retains rolling last four plus monotonic deadline; only native sender bound to the grant is accepted by browser. Status does not grant permission without REST state and native activation.

## Capture/display descriptor

Host-binding REST carries:

```json
{"display_id":"opaque-native-id","capture_generation":3,"track_sid":"TR_example",
 "pixel_width":1920,"pixel_height":1080,"rotation":0,
 "desktop_bounds":{"x":-1920,"y":0,"width":1920,"height":1080},
 "dpi_scale":1.25,"coordinate_space":"physical_pixels","source":"display"}
```

Display ID is not a device fingerprint exposed to room strangers; only authorized pair sees descriptor. Native OS adapter owns geometry. Browser uses rendered video intrinsic dimensions and CSS contain rect: scale=min(viewWidth/frameWidth,viewHeight/frameHeight), offsets center; subtract letterbox, reject out-of-image points, invert local zoom/pan transform, normalize to [0,1]. Never multiply browser coordinates by devicePixelRatio. Native capture adapter supplies an upright, unmirrored image of the selected display; encoder downscaling preserves that orientation. `rotation` describes the original OS display rotation for diagnostics, not a second frontend rotation. Native maps x=left+round(u*(width−1)), y=top+round(v*(height−1)) in that upright physical display space. The adapter normalizes any raw rotated capture buffers before publication, so browser and injection do not guess a rotation. Windows then normalizes against full virtual-desktop bounds for SendInput with virtual-desktop flag; macOS converts physical pixels to API coordinate space in its adapter; portal uses its stream coordinate system. Resize/rotation/topology/track replacement invalidates capture_generation and pauses then requires new locally approved binding/grant. Initial release controls one selected display; switching monitors ends old authority.

## Backend notification envelope (not authority)

New WSS `/ws/collaboration/v1/` after single-use ticket authentication receives `{v:1,event_id:uuid,type,resource_id:string,version:int}`. `type` is `grant.changed`, `device.changed`, `host_binding.changed`, `transfer.changed`, `session.ended`. No input, tokens, filenames or grants in broad room groups. Address per binding/account+context/device with server checks. Client refetches authorized resource on event; on gaps/reconnect fetch snapshot. Events may duplicate or arrive out of order; accept only increasing version. No write to frontend state can open native gate.

Ticket is passed in the first WS frame `{v:1,type:'authenticate',ticket:opaque}` within 5s, consumed once, bound to browser binding or device key and expires in 30s. Never put it in URL/subprotocol logs. Browser connection requires exact allowlisted Origin; native route is identified by device ticket and may omit Origin. Do not allow missing Origin to bypass browser authentication. Apply max frame 8KiB/rate 20 per second; new WS cannot send arbitrary input or consent actions. Pings/invalidation only; REST remains authoritative.

## Exact v1 keyboard code allowlist and capability mapping

Allow `KeyA` through `KeyZ`, `Digit0` through `Digit9`, `Numpad0` through `Numpad9`, `F1` through `F12`, plus: `Backquote`, `Minus`, `Equal`, `BracketLeft`, `BracketRight`, `Backslash`, `Semicolon`, `Quote`, `Comma`, `Period`, `Slash`, `IntlBackslash`, `Space`, `Enter`, `Tab`, `Backspace`, `Delete`, `Insert`, `Home`, `End`, `PageUp`, `PageDown`, `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`, `Escape`, `ShiftLeft`, `ShiftRight`, `ControlLeft`, `ControlRight`, `AltLeft`, `AltRight`, `MetaLeft`, `MetaRight`, `CapsLock`, `NumLock`, `NumpadAdd`, `NumpadSubtract`, `NumpadMultiply`, `NumpadDivide`, `NumpadDecimal`, `NumpadEnter`. Others return unsupported_key and pause; do not map arbitrary numeric scan codes. Host reports its supported subset before requesting keyboard capability.

`move→pointer.move`; `button→pointer.button` plus pointer.move for its position; `wheel→pointer.scroll` plus pointer.move; `key→keyboard.physical`; `text→keyboard.text`. Reset/heartbeat are always accepted from the bound controller while a grant exists, but cannot open a closed gate. Modifier/chord rejection happens against the proposed resulting pressed set before injection. Prohibited chords include native Stop chord, Ctrl+Alt+Delete, Meta+L on Windows, Control+Command+Q on macOS; locked/secure OS contexts remain denied independently of this finite list. No protocol-level promise to block every possible application action.
