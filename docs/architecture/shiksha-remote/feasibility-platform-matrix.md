# B/J. Feasibility and platform compatibility

Research date: 2026-10-12. “Feasible” means documented native capability, **not a tested Shiksha release**. Source keys link through [sources](sources.md).

## Browser and integration options

| Option | Installation / reach | Latency, footprint and operations | Decision |
|---|---|---|---|
| Website only | No install, modern browsers | Can send events and capture user-selected screen; DOM events cannot inject into arbitrary desktop apps | Controller/viewer only |
| Standalone Rust companion + outbound Django/LiveKit | One installer; all browsers that run existing Shiksha Live | SFU input path; one additional room connection; libwebrtc and encoder dominate size; no inbound firewall/NAT mapping | **Recommended** |
| Extension + native messaging host | Native installer **plus** store extension/enterprise deployment; browser-specific manifests | Authenticated extension↔host pipe, clear allowed extension IDs, can avoid a localhost listener; still needs same OS permissions and remote transport | Legitimate but extra support/release surface [S7] |
| Browser↔localhost HTTP/WS bridge | Native installer, browser trust/permission handling | Avoids native WebRTC bundle if browser relays data, but browser suspension stalls control; must prove source mapping and local service security | Not MVP |
| Native capture + separate remote desktop stack | Another transport, screen implementation and server policy | Can improve dedicated remote-desktop tuning but duplicates working LiveKit capabilities | Reject unless measured SDK blocker justifies ADR revision |
| Tauri shell + Rust | Native installer and platform webview | UI reuse and accessibility potential; system webview/runtime patching/dependencies; does not remove WebRTC size | Fallback after native UI accessibility spike |
| Electron shell | Bundled browser runtime | Familiar web UI but larger distribution/runtime budget; no unique permission benefit | Not justified |

Browser controller baseline: current stable desktop Chrome and Edge first; Firefox and Safari after real keyboard/focus/media tests. All continue ordinary livestreams even without control support. No cross-browser promise for browser-reserved shortcuts, Keyboard Lock, IME or filesystem-save APIs. Extension alone never supplies OS input.

## Why no localhost bridge

HTTPS pages cannot be assumed able to connect to arbitrary HTTP or `ws://` local services. Mixed-content exceptions, loopback treatment, browser Local Network Access prompts, CSP and enterprise policies differ. Chrome documents an LNA permission rollout [S8]; that is not evidence that every WS/browser combination behaves identically. Local TLS also creates certificate provisioning issues.

If a later ADR adds a bridge: bind loopback only (IPv4/IPv6), reject noncanonical Host and null/unapproved Origin, forbid wildcard CORS, authenticate every operation with a one-use origin-bound challenge, use explicit CSRF protection, reject DNS rebinding, do not expose control at a GET URL, rate-limit probes, and test actual browser versions. CORS is neither authentication nor WebSocket authorization. An untrusted site can attempt local requests even when it cannot read responses. Never install a root CA merely to make the bridge work.

The recommended browser gets companion status **from its authenticated Shiksha API**, scoped to its account and active host binding. “Device online” does not establish it is the computer running this browser. User selects a named paired device and confirms natively on that device. Optional `shiksha-remote://open` only launches the UI; it carries no credentials, controller, or auto-approve command. Manual launch always works.

## Host capability matrix

| Platform | Real input | Capture | Permission / limits | Planned status |
|---|---|---|---|---|
| Windows 11 | Win32 SendInput, physical scan codes/Unicode text [S4] | Windows Graphics Capture with local selection [S5] | User-level process; UIPI blocks higher-integrity apps. No UAC secure desktop, lock screen or Ctrl+Alt+Del control; no UIAccess/elevation | First certification target, x64 |
| Windows 10 | Same API family; WGC version compatibility required | WGC on sufficiently recent builds | Exact supported/serviced build and runtime dependencies must be recorded; do not claim every Windows 10 installation | Later compatibility gate |
| macOS | CGEvent posting with Accessibility trust [S6] | ScreenCaptureKit; Screen Recording authorization [S6] | Per-user TCC grants, signed/notarized identity, secure input/protected contexts, point/pixel transforms; no password/lock-screen bypass | Second native platform |
| Linux X11 | XTEST fake key/button/motion within authorized X display [S10] | X11 capture adapter | X11 has weak inter-client isolation; local display authorization still required. Do not run root or grant global uinput access | Limited certified distro/Xorg build |
| Wayland GNOME / KDE | RemoteDesktop portal selects devices; ConnectToEIS with libei preferred, portal Notify methods only where supported [S9] | Same portal session + PipeWire streams | Compositor/backend/version-specific; screen sharing support alone proves nothing about input. No XTest for arbitrary Wayland apps | Capability-probed experimental until individual validation |
| wlroots and other Wayland | Only available permissioned RemoteDesktop backend | Backend-dependent PipeWire | A ScreenCast-only backend is insufficient. Never fall back to privileged uinput/virtual-keyboard bypass | Unsupported when required portal absent |
| Pop!_OS COSMIC | RemoteDesktop + libei implementation merged Aug 19, 2026; compositor EI dependency also merged [S11] | Combined portal screen selection needs installed-version test | Upstream merge does not prove a particular Pop!_OS package contains it. Runtime inspect RemoteDesktop version/device mask/ConnectToEIS, consent and capture stream mapping | Candidate, not certified; no blanket “Wayland unsupported” claim |

Use portal `persist_mode=0` and no restore token for assistance. Platform permissions may persist (Accessibility etc.), but Shiksha consent never does. Once using EIS, do not mix portal Notify input methods into that same session [S9]. On permission loss, lock, sleep, display removal or session switch, stop input and require a new grant.

## Mobile

The repository contains Flutter, not Expo. Preserve `shikshacom_app/lib/features/live/classroom/` and `screen_share_service.dart`. Ship join/view and existing features unchanged. Browser/native mobile recipient transfer is a later bounded adapter with explicit save UI. Mobile controller touch/keyboard is optional, tested independently; do not call it desktop parity.

iOS ReplayKit supports capture/broadcasting, not a general public API for another user to inject arbitrary OS input [S12]. Android MediaProjection requires consent and foreground-service handling; it is capture, not input authorization [S12]. Android accessibility/device-management approaches have additional platform/distribution constraints and are out of scope. No iOS/Android controlled-device feature in this roadmap.
