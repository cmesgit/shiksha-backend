# Primary research sources

Accessed 2026-10-12. API support in upstream docs is not proof of support in checked-in SDK versions. Proposals/targets in this pack are original design decisions, not vendor guarantees. No benchmarks were obtained from these pages.

| Key | Primary source | Used for |
|---|---|---|
| S1 | [LiveKit data packets](https://docs.livekit.io/transport/data/packets/) | Reliable ordered best-effort delivery, lossy transport, selective destinations, packet sizes |
| S2 | [LiveKit byte streams](https://docs.livekit.io/transport/data/byte-streams/) | sendFile/streamBytes, receiver handlers, progress, targeted streams; not persistent storage |
| S3 | [LiveKit RPC](https://docs.livekit.io/transport/data/rpc/), [Rust SDK](https://github.com/livekit/rust-sdks), [NativeVideoSource](https://docs.rs/livekit/latest/livekit/webrtc/video_source/native/struct.NativeVideoSource.html) | RPC/timeouts; native RTC/media feasibility, not turnkey certified desktop capture |
| S4 | [Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput) | Input injection, UIPI, already-held keys |
| S5 | [Microsoft screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture) | Windows Graphics Capture |
| S6 | [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit), [Accessibility trust](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions), [CGEvent posting](https://developer.apple.com/documentation/coregraphics/cgevent/post(tap:)) | Native macOS capture/input and permission integration |
| S7 | [Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) | Extension + separate host architecture |
| S8 | [Chrome Local Network Access](https://developer.chrome.com/blog/local-network-access) | Local network permission changes; not universal localhost availability |
| S9 | [RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html), [libei](https://libinput.pages.freedesktop.org/libei/) | Permissioned Wayland input and combined screencast sessions |
| S10 | [XTEST protocol](https://xorg.freedesktop.org/archive/X11R7.6/doc/xextproto/xtest.html) | X11 event injection |
| S11 | [COSMIC RemoteDesktop PR #317](https://github.com/pop-os/xdg-desktop-portal-cosmic/pull/317), [portal repository](https://github.com/pop-os/xdg-desktop-portal-cosmic) | Merge dated Aug 19, 2026; libei/legacy Notify methods; installed capability still unverified |
| S12 | [Apple ReplayKit](https://developer.apple.com/documentation/replaykit), [Android MediaProjection](https://developer.android.com/media/grow/media-projection) | Mobile capture limitations |
| S13 | [LiveKit encryption](https://docs.livekit.io/transport/encryption/) | Opt-in SDK encryption and key distribution responsibilities |
| S14 | [LiveKit pricing](https://livekit.com/pricing) | Snapshot in performance-cost analysis; verify account quote at launch |
| S15 | [Django 6 supported Python](https://docs.djangoproject.com/en/6.0/faq/install/) | Docker Python 3.11 inconsistency |
| S16 | [RFC 9449 DPoP](https://www.rfc-editor.org/rfc/rfc9449) | Device proof-of-possession contract |
| S17 | [Tauri Windows distribution](https://v2.tauri.app/distribute/windows-installer/) | WebView runtime dependency tradeoffs |

Source-derived descriptions are deliberately brief. Detailed authorization, API, state, testing and rollout specifications elsewhere are Shiksha design proposals.
