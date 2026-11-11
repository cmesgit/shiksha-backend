# H. Browser-first UX and integration specification

Existing Live UI remains the entry point; no new meeting route. Place **Assistance** next to existing screen controls, progressively disclosed only for interactive participants in enabled session kinds. Add **Send privately** to existing Files panel, keeping Room files. A user without a companion continues audio/video/chat/screen sharing normally.

## Components and source ownership

Proposed canonical `shared/src/components/live/assistance/`: `AssistanceProvider.jsx`, `AssistanceMenu.jsx`, `CompanionSetupDialog.jsx`, `ControlRequestDialog.jsx`, `ControlStatusBar.jsx`, `RemoteInputSurface.jsx`, `PrivateTransferPanel.jsx`, `assistance.css`. Proposed `shared/src/api/collaborationService.js`, `shared/src/hooks/useCollaboration.js` and `shared/src/collaboration/protocol.js`.

Use `shared/sync.mjs` target entries deliberately. Existing dashboard GroupSessionLive/GroupSessionControlBar are shared; main-site GroupSessionLive and all GroupSessionClassroomUI variants remain per-app wrappers. Inject a shared provider/control surface into these wrappers; do not overwrite entire files or unify unrelated classroom behavior. FilesPanel stays functional during migration. Resolve six existing sync drifts before bulk generation, or copy/check only newly registered files with a reviewed scoped-sync change. Admin remains outside shared-live targets. Do not modify auth/route/billing behavior to add UI.

## User-visible states

| State | Host view | Controller view | Allowed action |
|---|---|---|---|
| Ordinary screen share | “Sharing screen” | “Viewing …” | Request setup if owner enabled assistance; no input overlay active |
| No companion | Install / open instructions + normal Live controls | Assistance unavailable | Official OS installer, manual launch, pair code |
| Paired but offline | Named device “Offline” | Unavailable | Launch app; status from server, no speculative localhost detection |
| Online unbound | Select device; “Allow remote assistance” | Unavailable | Native display/share confirmation |
| Ready/share bound | “Display shared; control not allowed” | Request Control | Select owner’s verified screen, explicit capabilities |
| Requested | Named request dialog with device/display/capabilities | “Waiting for Alice” + cancel | Deny default focus; Allow opens native consent step |
| Approved | “Confirm on your computer” | “Waiting for local confirmation” | Native physical Allow or Deny; 30s deadline |
| Active | Persistent native + browser banner with controller name | Clearly framed input surface; shortcut toolbar | Stop, Pause, finish; files remain separate consent |
| Paused | “Control paused — input stopped” | View-only, no keys captured | Host browser resume intent + native reapproval |
| Revoked/expired/failed | “Control stopped” with reason | “Permission ended” | Fresh request only; normal livestream remains |
| Reconnecting | “Control stopped during reconnect” | Existing reconnect banner plus view-only | Restore meeting, never grant automatically |

Request button never implies role eligibility. Show a teacher and a student identically when authorized. Host can decline or mute future requests for this meeting; persist in Redis host binding and apply server side. Offers/requests privately target involved users, no full-room toast. Public viewers/spectators see no control/file-request buttons.

## Consent copy and accessibility

Browser: “Allow **{verified name}** to request control of **{device} — {display}**? They can use the mouse and keyboard in applications you can access, including files and terminals. Other participants who can view this screen may see it. File sending requires separate approval. Confirm on your computer to start.” Show capability checkboxes, expiry and Stop instruction. No prechecked unattended/trust-forever option.

Native prompt repeats identity/room/device/display/capabilities from authoritative server record and includes “Stop anytime with [configured physical shortcut] or Stop control.” Display selected screen preview locally. Show recording status when verified; refuse MVP activation if policy unknown or public broadcast. Avoid long-term invisible background sharing. Pairing confirmation names the account, not merely “Device connected”.

Dialogs use semantic role=dialog/aria-modal, labelled title/body, focus trap, Escape denies/cancels, focus returns to initiating control; default focus Deny, no countdown auto-approve. Screen reader announces state changes via polite live region; stop/error uses immediate announcement without focus theft. Buttons ≥44 CSS pixels, visible focus, sufficient contrast, non-color status labels. Native UI requires separate keyboard/screen-reader tests; web accessibility does not certify it.

## Input surface behavior

Only capture keyboard when controller deliberately focuses the verified remote-display surface during active control. A persistent “Keyboard goes to Alice’s computer” label and local release shortcut remain visible. Do not intercept typing in chat, file dialogs, URL bar, permission dialogs or hidden tabs. On blur, visibilitychange, pagehide, pointercancel, lost pointer capture or transport change send release/pause best-effort; native timeout protects lost messages. Physical keys unsupported by browser/host show a clear disabled shortcut action, never pretend success.

Pointer: use contained-video content rect and invert zoom/pan; reject letterbox clicks. Pin verified companion track SID; do not select `first ScreenShare` from arbitrary room tracks. Preserve multiple screen tiles; one active input surface only. Host display/track generation change immediately disables interaction. Multi-monitor selection occurs locally on the host; controller can request a change but cannot silently expand scope.

Zoom/fullscreen affect viewing transform only, not capture/OS dimensions. Default readable1080p15 starting target; native pixel dimensions shown, offer 1:1 and fit views. Measure text readability/latency. Existing classroom adaptive streaming remains default when assistance inactive. During active control prioritize readable screen subscription and keep video visible; when hidden pause input. No auto subscription to everyone’s highest resolution.

## Screen capture handoff

Owner initially shares normally. On Allow Assistance, explain that full-display capture is needed and current window/tab share is view-only. Native local picker/preview selects display; after verified track ready, show handoff action and stop browser screen track. Never force `getDisplayMedia` without user gesture. No two full-resolution captures run long-term. Native capture status is visible, and Stop sharing closes binding/grants independently of meeting audio/video. If native capture fails, keep meeting and ordinary sharing usable; do not restart camera/screen without consent.

## File panel

Recipient picker lists current eligible interactive participants by name/context; one recipient per offer. Preview sanitized name, size, sender, unverified type and scan status. Explicit Accept/Reject; no auto-download. Show hashing, waiting, sending/receiving bytes, verifying, ready-to-save, cancelled and failed states. Distinguish sender100% from verified receipt. Cancellation never closes room. Starting control defers/cancels realtime transfers for those participants to protect input latency; explain this briefly in UI. Durable path offers “Send after scan” when available.

## Session kinds / mobile

MVP instant + scheduled GroupSession with verified interactive membership. PrivateSession uses existing accepted membership in later phase. LiveSession classroom needs explicit interactive-assistance opt-in; microphone-only students must not silently gain general screen publishing rights. Companion screen grant is separately scoped to its locally confirmed host binding. Public broadcasts/hidden spectators never eligible. SkillSession has a separate adapter and identity format. Flutter stays unchanged initially; later view/files integration uses the same v1 contracts. Mobile desktop control remains a separately tested optional interface.
