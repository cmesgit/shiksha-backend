# C. Architecture decisions

Decisions are proposed for architecture-owner review; implementation contracts in this pack are the default. Change them through an ADR, not ad hoc coding-agent choices.

## ADR-01: Extend existing collaboration, never meeting identity

Use `sessions_app` for collaboration services and existing RemoteControlGrant. A typed `SessionRef {kind,id}` resolves group, private, live and skill through a common **authorization adapter**, not a new database meeting table. Group/instant launches first. The adapter must preserve all existing course, batch, learner profile, guest entitlement, lobby and removal gates. Unknown contexts are disabled. Public audience/spectator membership never authorizes a request.

## ADR-02: Server-assisted coordination, native endpoint

```mermaid
flowchart LR
  B[Controller: existing React Live UI] -->|cookie auth + CSRF / decisions| D[Django collaboration service]
  H[Owner: existing React Live UI] -->|host binding + browser consent| D
  D <--> P[(Existing PostgreSQL)]
  D <--> R[Existing Redis / Channels]
  C[Rust companion + native consent + input gate] -->|outbound HTTPS / authenticated WSS| D
  B <-->|targeted input / display video| L[Existing LiveKit Cloud room]
  H <-->|existing audio / video / chat| L
  C <-->|screen-only participant / data| L
  C -->|validated SendInput| O[Ordinary user desktop]
```

No high-frequency input goes through Django. Control-plane traffic uses existing ASGI/Redis. An isolated collaboration WebSocket is justified because its lifecycle/credentials differ from chat and must not change attendance or keep meetings alive. It is additive within the same backend, not another service. Existing chat sockets continue untouched except legacy control events cease granting authority.

Companion LiveKit identity: `rcdev:<device_uuid>:<binding_uuid>` generated server-side. It is visible as “Alice’s desktop — Remote Companion”, tied to Alice in UI, excluded from human counts/attendance/billing entitlement and normal people controls. Actual LiveKit cloud connections remain billable. Do not use a hidden participant. **Native token (revised 11 November 2026 — supersedes the original grant):** `room_join` true; `can_publish` true with screen_share only; `can_publish_data` true; **`can_subscribe` true**; no microphone, camera, admin, recorder or agent privilege. Tokens have 60-second join TTL; validity of an existing connection is not bounded by that TTL, so native leases remain mandatory.

The original grant said `can_subscribe: false` "for media". **That participant cannot exist.** LiveKit has no separate grant for receiving data — `canSubscribe` governs tracks generally, and upstream treats data surviving its revocation as a bug — so the companion as first specified would have received no input at all. Evidence: [evidence/sdk-capability-findings.md](evidence/sdk-capability-findings.md). Options and trade-offs: [adr-02-revision-proposal.md](adr-02-revision-proposal.md), Option C adopted.

**Media isolation is therefore client-side, and that must be stated plainly rather than implied away.** The companion connects with auto-subscribe disabled, subscribes to no track, and instantiates no audio sink or decoder. A server-signed claim no longer enforces this; our own code does. That is an acceptable trade *only* because the threat model already places a maliciously modified companion outside the guarantee — the token never defended against our own binary. It defended against something else holding the credential, and that risk is unchanged and now carries its own threat-register row.

**Authority lives in the payload, not in the SDK's sender field.** The companion authorises input on the signed epoch, nonce freshness and sequence floors it can verify itself; SDK-reported sender identity is defence-in-depth, used when present. This is deliberate: the Rust SDK drops the sender whenever participant lookup misses, including the ordinary join race, and the upstream fix is unmerged. A packet the SDK cannot attribute is rejected on its own merits — for failing verification, not for lacking a name — so the invariant no longer depends on an upstream release.

**Still to verify in RC-003:** that the Rust SDK exposes an auto-subscribe-off connect option and that declining subscription is durable. If it is not, fall back to Option B (input over the existing authenticated Django WebSocket), which restores server-enforced isolation at a capacity cost on infrastructure already identified as the bottleneck.

## ADR-03: Capture ownership provides coordinate safety

A browser `getDisplayMedia` track cannot reliably identify the physical display or OS coordinate origin. A tab/window image cannot safely map to arbitrary applications. No heuristic based on matching resolutions or user-entered offsets in the MVP.

For assistance, local companion selects **one complete display**, shows scope, starts native capture and publishes a screen-share track in the existing room. UI swaps focus to that verified track; browser screen sharing is explicitly stopped only after a visible handoff. Track SID, capture generation and display mapping are bound to the host binding and grant. Other meeting tracks remain unchanged. All usual room viewers can see this full display; consent must disclose that. If a room is publicly broadcast or recording status is unknown, disable assistance for MVP; do not imply pair-private video.

## ADR-04: Independent local consent + expiring authority

Browser approval is intent; local native Allow is the last authorization step. The companion fetches the authoritative request, validates signed activation and a current lease, binds to a display it captured, then opens its input gate. Local pause/stop and physical emergency controls are enforced before processing queued network messages. One controller/device across all rooms. Account/admin privilege never grants local control. A local-stop latch cannot be reopened by a stale server response.

## ADR-05: Minimal UI/core, bounded platform expansion

Rust workspace with a protocol/state crate, platform adapters and native shell. Windows MVP uses Win32 native controls via the `windows` crate so accessibility and trusted physical event handling can be tested without a browser webview. No requirement for every OS to share its UI toolkit. macOS native UI and Linux toolkit adapters reuse the core. Tauri is a fallback if measured maintenance/accessibility costs favor it; never load remote web content into a privileged shell.

## ADR-06: Files are a separate capability

Remote-control permission never grants filesystem APIs, clipboard sync or automatic uploads/downloads. A controller can nevertheless operate Explorer/VS Code/terminal through normal UI and can read/change user-accessible files: **that is inherent in genuine desktop control**. Do not advertise it as an application sandbox. No hidden command execution channel, remote file enumeration, shell API, privilege escalation or unattended access.

Existing room files retain their meaning. Add private peer transfer consent/metadata and targeted streams. Native file transfer adds little MVP value; defer until browser limits are measured. Optional durable files reuse existing private media authorization patterns and async jobs but require a recipient ACL and quarantine storage.

## ADR-07: Do not promise E2EE by using WebRTC

TLS and WebRTC transport encryption protect links; LiveKit/backend are trusted infrastructure in MVP. Current LiveKit documents opt-in E2EE features [S13], but SDK interoperability and key distribution are not implemented here. Do not publish a blanket E2EE claim for media, packets, files or signaling. Targeted delivery prevents normal room recipients receiving payloads, not the SFU inspecting them. Optional future E2EE needs a separate key-management/recovery/recording ADR.

## Open decisions requiring a named human owner

These are **not** decided by this pack. An ADR records an engineering choice; the items below are product, policy or safeguarding choices that an architecture reviewer is not the right person to settle alone. Each names what it blocks. Do not let an implementation task resolve one by default.

### OD-01 — **RESOLVED 11 November 2026.** Split by context.

**Decision:** eligibility depends on the session kind, not on a single platform-wide answer.

| Context | Who may request control |
|---|---|
| **Instant collaboration** (general `GroupSession`, `session_type="instant"`) | **Anyone the host admitted**, including an un-authenticated `InstantCollaborationGuest`. No role test, no account test |
| Scheduled `GroupSession`, `LiveSession` classroom, course contexts | **Teacher-initiated only** — matches what `sessions_app/remote_control_views.py` already enforces |
| `PrivateSession`, `SkillSession` | Unchanged; still disabled until RC-020 / RC-022 |
| Public broadcast / spectator | Unchanged; audience confers no rights |

The reasoning is that a collab is a room two or more people *chose* to be in, with symmetric standing, while a classroom carries an authority gradient and an institutional duty of care. Peer help is the point of the former; in the latter it would put a minor under pressure from both a peer and the setting.

**This is the most permissive configuration of the three that were offered, and the consequences are load-bearing rather than theoretical:**

1. **`InstantCollaborationGuest` is identity-free.** Its model docstring says "a temporary, room-scoped visitor; never an account or group invite" — the whole identity is a self-chosen `name` of up to 80 characters. So a control session in a collab may have **no attributable requester**: nobody to ban, nobody to audit beyond a display string somebody typed.
2. **Lobby admission is now an authorization decision, not a courtesy.** Admitting a guest to a collab makes them eligible to ask for the host's desktop. The admit UI must say so in plain words — a host clicking "Admit" to let someone watch, and thereby granting them standing to request desktop control, is exactly the kind of silent widening this pack exists to prevent. **This is a hard requirement on RC-011, not a nicety.**
3. **The consent dialog carries everything.** With an unidentified requester there is no account history, no prior relationship and no post-hoc remedy. T22's deny-by-default, dwell timer and plain-language capability copy are the only controls left standing, so they are not negotiable in this context, and T24 covers what remains.

**Unblocks** RC-011, RC-016 and RC-021. RC-002's authorization adapter must keep the requester predicate **per-context** rather than global — the matrix above is the predicate, and hardcoding either answer breaks the other half.

### The original framing, kept for the reasoning

The rest of the pack assumes yes — ADR-01 authorizes through participant membership, "never teacher privilege", and the acceptance scenarios demand a Student A↔Student B demo. That is defensible as architecture: it avoids baking a role hierarchy into a consent system, and teacher-initiated control carries its own coercion problem, arguably a worse one given the authority gradient.

It is still an unmade decision, because the existing code deliberately went the other way. `sessions_app/remote_control_views.py` requires real teacher context and documents why. Widening it means a K-12 platform ships a mechanism for one minor to operate another minor's computer, with the consent dialog as the only thing in between. The relevant risk is the coercion case in [permission-security-model.md](permission-security-model.md), which no control in this pack reduces.

Options, honestly stated: (a) peer-to-peer with the hardened consent dialog, (b) teacher-initiated only for MVP, deferring peers until there is incident data, (c) peer-to-peer gated on an admin or guardian setting, defaulting closed.

**Blocks:** RC-011 (consent UI), RC-016 (end-to-end acceptance), RC-021 (classroom policy). RC-002 can freeze schemas without it provided the authorization adapter keeps the requester-role predicate pluggable rather than hardcoding either answer.

### OD-02 — **RESOLVED 11 November 2026.** 90 days, and the deletion job is in scope.

**Decision:** consent, grant and device-lifecycle audit records are retained **90 days**, then deleted automatically.

Ninety rather than thirty because safeguarding complaints arrive late — a child tells a parent weeks after the fact, and a window that has already closed turns an investigable incident into one person's word against another's. Ninety rather than indefinite because these are records about minors, and every additional month is one more month they can leak or be misused.

**This is a promise that requires building, and that is the point of recording it here.** `accounts/settings_models.py:169` is explicit that `purge_after` exists but *"Nothing purges automatically yet"*, and no Celery beat entry deletes any audit class. The platform currently retains everything forever. So a documented 90-day policy with no deleter would be a claim the system actively contradicts — worse than silence, because it reads as a guarantee.

**RC-030 therefore gains a deliverable:** a scheduled deletion job for collaboration audit records, plus a test that creates a record older than the window, runs the job, and asserts the row is gone. A retention policy whose enforcement is untested is a retention policy that silently stops working.

Scope note: this covers collaboration audit rows only. The wider platform-retention problem — `AccountDeletionRequest.purge_after` read by nothing, `AuthEvent`, `UserSession`, `SmsLog` retained indefinitely — is **not** fixed by this decision and remains open elsewhere.

### OD-03 — **RESOLVED 11 November 2026.** Fail closed; the current behaviour is the shipped behaviour.

**Decision:** assistance stays **disabled** when a room is publicly broadcast or when recording status cannot be determined. ADR-03's position was a placeholder; it is now the decision.

The reasoning is that if the system cannot tell whether a desktop is being recorded, it must not let that desktop be controlled. The alternative — allow it and disclose in the dialog — pushes yet more weight onto the one control T22 already identifies as carrying too much, and does so in exactly the case where the consequences are most permanent: a recording outlives the session, the grant and any later revocation.

Cost accepted knowingly: assistance is unavailable in rooms whose recording state is merely *unknown* rather than actually on. The three-way refinement (known-off allows, known-on discloses, unknown denies) is a reasonable future change, but it depends on the recording-state signal being trustworthy, and nobody has verified that it is.

**All three open decisions are now resolved.** The section is kept because the shape of the questions — and the fact that they were held open rather than defaulted by an implementation task — is the part worth imitating next time.
