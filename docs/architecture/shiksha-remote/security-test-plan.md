# N. Validation and test plan

Documentation-only stage: no platform is certified and no end-to-end control is claimed. Existing tests are evidence of test coverage intent, not of a successful run this turn. Use [validation-report](validation-report.md) for checks actually performed.

## Test layers and evidence

| Layer | Automated coverage | Evidence / release gate |
|---|---|---|
| Existing regressions | Existing livestream lifecycle/profile/batch/moderation tests; sessions_app private/group join/end/socket tests; config media ACL tests | Before/after result, environment/dependencies, no known failures hidden |
| Unit | State transition table; exact capability subset; UUID serializers; typed session adapter; lease nonce/monotonic deadlines; file validation | Python/JS/Rust shared fixtures, invalid cases included |
| PostgreSQL integration | Concurrent approval across rooms; partial unique indexes; rollback/idempotency; stale request approval; quota reservations | Real Postgres matching declared supported release, not SQLite proof |
| Auth integration | Cookie CSRF + Origin; DPoP/nonce/replay; wrong device, expired/reused refresh; wrong profile; auth sid revoke | API tests for every contract row; no token logs |
| LiveKit staging | Existing join identity/claims remain; companion directed packets; media no-subscribe; track SID binding; receipts/SDK cancellation | Pinned browser/Rust versions, test room, recorded metadata-only results |
| Browser UI | Dialog accessibility/focus/deny default; input surface activation, letterbox/zoom/fullscreen; file consent before sender byte API | Playwright or existing approved harness; test all three web apps |
| Native unit/fuzz | Strict parser, random ordering/rate floods, state ownership/release, capture generations, fake clocks | cargo test + fuzz corpus; no parser panic opens gate |
| Native integration | Real OS apps and capture; physical stop; injected-click rejection; lock/UAC; watchdog on kill | Actual Windows11 desktop first, named versions/hardware |
| Cross-platform | macOS TCC + Retina; X11; portal GNOME/KDE; COSMIC installed versions | Separate support decision for each matrix row |
| Operations | Flag-off, rollback, outdated client, compromised updater mirror, scanner fail, orphan cleanup | Staging drill with proof no retained authority |

Existing test modules to retain: `livestream/tests.py`, `tests_lifecycle_hardening.py`, `tests_webhook_races.py`, `tests_attendance_profiles.py`, `tests_moderation_identity.py`, `tests_end_reconciliation.py`, `sessions_app/tests.py`, `tests_group_session_end.py`, `tests_group_session_socket.py`, `config/tests_media_security.py`. Determine current runner/settings via RC-001; do not run production DB migrations to satisfy a local test.

## Mandatory abuse cases (T01–T24)

Threat IDs and expected enforcement are in [permission-security-model](permission-security-model.md). Every task touching a boundary adds applicable cases. Include network-level fault injection, not only mocking the frontend:

- Approve two controllers on one device in separate rooms at same instant: one succeeds; loser409; no duplicate local controller.
- Replay a valid packet under new epoch, old LiveKit SID, different grant/device/capture generation or another room: zero OS calls.
- Revoke with companion unable to receive WS/data messages but still receiving queued input: stop by monotonic lease deadline, reject delayed successful lease reply.
- Remove user, expire guest entitlement, end session or revoke auth sid during active grant: stop; cannot refresh around the policy.
- Kill owner browser, controller browser, companion process and watchdog independently while holding Shift/drag. Demonstrate release, or block release certification; no “cleanup in finally” assumption.
- Inject pause/resume and click native Allow through remote events: local consent cannot be forged.
- Lose key-up/reorder pointer/button between reliable and lossy lanes: release safely; no stale-pointer click.
- Flood malformed payloads/challenges beyond queue limits: indicator and physical Stop remain responsive.
- Tamper display geometry/track metadata or replace screen track with equal resolution: refuse mapping, never inject into unseen desktop.
- Transfer with no offer/acceptance, wrong recipient/SID/topic/hash/size, MIME spoof, path traversal, Unicode direction override, file cancelled at99%: no unauthorized save, no completed status.
- Scanner timeout, EICAR test fixture, zip bomb budget, expired object signed URL, repeated multipart completion: quarantine and quota correctness.
- T24, unattributable guest requester: an `InstantCollaborationGuest` — identity is a self-chosen display name, nothing more — requests and receives control in an instant collab. This is **permitted** by OD-01, so do not assert it is blocked. Assert the controls that replace identity: the lobby-admit UI disclosed that admitting confers control eligibility; the audit row is complete and reconstructable without an account (guest UUID, display name, admitting host, session, duration); removing the guest from the session terminates an active grant; and the same guest rejoining under the **same display name** is treated as a new identity with no inherited standing. The last one is the trap — a name is not a principal.
- T23, stolen device credential: replay a captured companion credential from a client that is **not** our companion and attempt to subscribe to room media. It will succeed — the revised ADR-02 token grants `can_subscribe: true` and isolation is client-side. Assert instead what the design actually promises: the rogue participant is **visible** in the room under its own identity, revoking the device terminates it, and the 60-second join TTL bounds reuse of a stale token. A test that asserts the subscribe is blocked is testing the superseded design and will fail for the wrong reason.
- T22, coerced consent: assert the dialog opens with Allow disabled and unfocused, that Enter/Space cannot approve before the dwell timer elapses, that the copy names the real capability rather than a euphemism, and that the audit row identifies requester, approver and duration. This case is only partly automatable — pair it with a scripted peer-pressure walkthrough judged by a safeguarding owner. A green assertion here is evidence about the dialog, never evidence that consent was freely given.

## Real-device acceptance scenarios

Windows MVP must demonstrate Student A→Student B and B→A in an existing **instant collaboration**; teacher/teacher and teacher/student use identical consent. **OD-01 is resolved** ([architecture-decisions.md](architecture-decisions.md)): peer-to-peer is in scope for instant collabs and includes un-authenticated lobby guests, while scheduled/classroom contexts are teacher-initiated only. Acceptance therefore needs **both** legs demonstrated — a student↔student collab pair *and* a teacher-initiated classroom grant — plus a rejection case proving a student cannot initiate in a classroom context. A single peer demo no longer covers the matrix. Operate VS Code, type a synthetic snippet, navigate a folder, scroll, select/drag, double-click and save. Local owner moves pointer and stops mid-keystroke. A screen cursor overlay alone fails acceptance.

Test negative monitor origin, portrait display,125/150/200% DPI, mixed-DPI dual displays, full-screen vs fit vs zoom, monitor unplug, resolution change and capture/source switch. Pointer error target≤2 physical pixels on fixed test markers before calling mapping supported. Keyboard layouts US, UK and at least one intended Indian-language IME; AltGr/dead keys, modifier release, browser-reserved shortcuts. Document unsupported combinations explicitly.

UAC prompt and elevated VS Code are expected **not controllable** by standard companion. Verify no bypass and clear feedback. Lock/unlock, fast-user-switch, sleep/resume and permission withdrawal require new authorization. macOS permission denial/revocation and signing identity updates, Linux portal cancel and missing RemoteDesktop interface must gracefully retain normal livestream.

Native accessibility: keyboard-only pairing/consent/Stop, screen reader identity/scope/status and visible focus. Emergency physical chord must work while a supported fullscreen app has focus. Native tray-only quit is insufficient as the only safety affordance.

## Reliability/performance evidence

Use the scenarios in [performance](performance-cost-analysis.md), publish trace summaries with no typed content. Verify event→inject/visual latency, bounded queue memory and lease expiration under CPU stalls. Physical stop timing gate target<50ms p95 is measured under load; OS suspension cannot guarantee wall-clock execution, so inject-time checks must reject expired events when resumed.

File byte accounting tests inspect actual sender calls/network instrumentation to show **zero content** sent before consent. A checkbox unit test is insufficient. Check cancelled streams do not tear down audio/video. Control activation drains existing file streams before freshness challenges permit input.

## Completion definition

Each task needs code review, meaningful tests, compatibility check, documented integration and rollback. Phase4 release needs a genuine OS control demo plus signed consent/revoke audit and negative tests. Phase7 needs independent security review, signed installers, staging rollback, load profile and explicit support matrix. A generated code patch or screenshot of a pointer does not meet done.
