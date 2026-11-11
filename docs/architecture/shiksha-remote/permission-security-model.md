# I. Permission, security and privacy model

## Trust boundaries and guarantees

Django authenticates users and paired devices, owns room policy and signed authority. LiveKit authenticates room connections and transports media/data; it does not decide desktop consent. Native core is the final input enforcement boundary. Local OS permissions bound what the companion can do. Browser UI state and every incoming data message are untrusted. A compromised host OS or maliciously modified companion is outside the guarantee; do not imply software can attest physical consent without a trustworthy local endpoint.

| Invariant | Primary enforcement | Independent check / test |
|---|---|---|
| No input without explicit owner consent | Native physical consent latch per grant/epoch | Browser decision alone fails; server issues activation only to correct paired device |
| Controller is actual participant | Django auth context + verified LiveKit identity/SID binding | Native compares SDK sender + current SID + grant principals, rejects server-origin data |
| Capability cannot widen | Server subset check / signed ticket | Native allowlist before each OS call |
| One controller/device | DB partial unique + device lock | Native single active epoch; no consent UI under active injection |
| Revoke survives lost notifications | Authoritative DB denies new leases | Native3s monotonic lease gate; local Stop closes gate immediately |
| Disconnect cannot restart control | Server terminal transition + new binding | Native discards epoch/queues on reconnect, no remembered consent |
| Protected OS contexts not bypassed | Same-user native process, platform APIs | No elevation/service/uinput bypass; platform denial terminates |
| File requires distinct consent | FileTransfer recipient decision | Receiver checks stream against own accepted offer before allocating/saving |
| Room/course scope preserved | Existing session adapters | Cross-room/batch/context deny fixtures, old join contract tests |
| Local user retains authority | Stop UI/hotkey, no keyboard blocking hooks | Physical movement/typing can pause; injected events cannot click own security dialogs |

Important limit: genuine desktop control permits actions available through ordinary UI, including commands in a terminal and viewing files/apps accessible to the user. Restricting protocol verbs does not sandbox those applications. Consent explicitly describes this power. OS integrity/secure desktop boundaries remain in force.

## Threat register

| Threat | Mitigation and residual risk | Required test |
|---|---|---|
| Forged request/client user ID | Server derives principals, interactive policy, origin+CSRF, throttles | T01 unauthorized actor, role/profile mismatch |
| Stolen account session | Recent auth for pairing/unlink; device local confirmation; grant native consent | T02 stolen browser session cannot pair/activate silently |
| Same-account child/teacher context confusion | Bind auth sid + active context + profile string; prohibit same-account control in MVP | T03 switch profile while request/active, terminate |
| Forged LiveKit packet or room metadata | SDK authenticated sender/SID plus local epoch/lease; no data-message permission | T04 stranger broadcasts copied grant ID |
| Replay/stale grant/clock change | Native nonce freshness + sequence floors + monotonic deadlines, signed epoch | T05 old packet, delayed lease, backward wall clock |
| Cross-room/device injection | Immutable room/bindings/device key/capture fields | T06 correct user, wrong room/device/SID |
| Double approval/multiple controllers | DB locks + conditional uniqueness; local single gate | T07 parallel approvals in two rooms |
| Missed revoke/partition/Redis outage | Native3s lease; backend never cache-authorizes through DB failure | T08 lost push, HTTP500, suspended network, late lease response |
| Host browser gone/native crash | Binding TTL, direct disconnect, native watchdog/release | T09 process kill while holding modifier/button |
| Reconnect or machine sleep | Terminal grant; no remembered epoch; fresh local approval | T10 reconnect all components independently |
| Local website attack/URI hijack | No listener; launch URI no credentials; both pairing confirmations | T11 evil-origin requests and forged launch parameters |
| CSRF/CORS/sibling subdomain takeover | Exact allowed Origins + explicit CSRF on cookie mutations; CSP; no wildcard origin | T12 cookies with missing CSRF and cross-site WS denied |
| Native dialogs approved by remote input | Close gate/flush before security dialogs; physical-event provenance test | T13 active controller attempts pause/resume/pair/update approval |
| Malformed/rate-flooded input | Strict parser/frame limits; bounded queues; per-source limits; semantic validation | T14 fuzz/NaN/oversize/sequence gap; native stays responsive |
| Display mix-up/private screen disclosure | Native display choice/preview, verified track+generation, handoff confirmation | T15 identical-resolution monitors, DPI/rotation/hotplug |
| Malicious recipient file | Explicit recipient selection; one identity; quota/stream cap; safe download | T16 unauthorized stream/header mutation/filename injection |
| Malware/bomb/quarantine bypass | Realtime unscanned label/restrictions; durable scanning fail closed | T17 EICAR fixture, archive expansion, scanner outage |
| Insecure update/supply chain | Signed metadata+artifact, pinned dependencies, antirollback, staging | T18 tampered/old/expired manifest, compromised mirror |
| Hidden monitoring/recording | Native indicator; scope disclosure; existing recording policy separately verified | T19 room viewer/recorder joins; no hidden companion identity |
| Lossy motion/race/stuck modifier | Reliable actions with positions, lane floors, heartbeat release-only recovery | T20 dropped/reordered move/button/keyup/focus events |
| Moderator/admin override | May terminate for safety; cannot consent or download private files implicitly | T21 staff account attempts activation/private download |
| **Coerced or socially engineered consent** | Deny-by-default dialog with no pre-selected Allow and no Enter-key default; consent copy names the actual power in plain language (ADR-06: the controller can read and change your files and run commands); minimum dwell before Allow becomes enabled; both parties named and continuously visible; owner Stop reachable at all times; audit retains requester, approver and duration. **Residual risk is not reduced by any control in this table: every cryptographic invariant holds perfectly while a pressured owner clicks Allow.** This is a product, policy and safeguarding control, not a technical one | T22 consent copy/dwell/default audit; scripted peer-pressure walkthrough reviewed by a safeguarding owner, not only by an engineer |

### On coerced consent

Every other row in this register assumes the attacker is outside the consent decision. This one assumes they are the person asking for it, in a context where the owner may be a minor and may know them. It is the most probable real failure of this feature and the least addressable by architecture.

Treat as consequences of that, not as optional polish: the consent dialog is deny-by-default with a dwell timer; the copy states the real capability rather than "allow remote assistance"; the session is visibly attributed to a named person throughout; and the audit trail answers "who asked, who approved, how long" without needing an incident investigation to reconstruct it.

**Student↔student control is the specific product decision this bears on, and it is not settled by this pack — see the open decision in [architecture-decisions.md](architecture-decisions.md).** The existing `sessions_app/remote_control_views.py` deliberately restricts requests to teacher context. Widening that is a safeguarding judgement with DPDP Act 2023 §9 implications for minors, parallel to the parent-anchored identity decision already made in `scholarship/`. It requires a human owner's recorded sign-off before RC-011 builds consent UI that offers it.

## Browser/session auth hardening scope

New collaboration routes enforce CSRF explicitly, not by assuming CookieJWTAuthentication does. Token bootstrap must use Django get_token, cookie/header validation and an exact allowlist: `https://app.shikshacom.com`, `https://teacher.shikshacom.com`, `https://shikshacom.com`, `https://www.shikshacom.com`. Add separate explicit localhost origins only in dev/test. Admin origin gains no desktop privilege. Browser websocket Origin required; device websocket ticket auth separate. Reject public query-string access tokens on new routes; preserve old socket compatibility until a separate migration.

CORS allows reads from trusted frontends but does not authorize requests. Authenticate each mutation and resource read; room UUID secrecy is not protection. Rate keys combine authenticated actor/device, target and IP; revocation bypasses ordinary throttles within a small abuse-safe budget so a spammed device can always stop. Existing log scrubbers must cover request bodies/query strings for new credential endpoints, JWT/DPoP headers and crash dumps.

## Privacy/data minimization

No key/text/pointer-payload logs, passwords, clipboard, screenshots or file bytes in telemetry. Audit consent/capability changes, paired-device lifecycle, terminal reasons, durations, app versions and coarse performance. File names appear only to involved parties and necessary storage metadata, not general analytics. Limit production debug dumps and encrypt/support-redact crash reports. Audit retention default90d; approve final policy before release, provide device/session history and unlink UI.

MVP claims transport encryption only, not E2EE or private screen visibility to just the controller. Native capture is a normal screen track in an existing room, potentially visible to approved room participants and any existing recording pipeline. Public broadcast and unknown recording status disable assistance until explicit policy/UX is available. Disabling control does not erase any previously recorded screen. Host can stop screen sharing separately.

## Review gate

Architecture-owner review required for auth adapters, CSRF, device proof, signed lease rules, migrations/concurrency, native physical-consent gate, input parsing, update trust and scanning. Review must include runnable abuse fixtures and real OS demonstrations. Feature flag defaults off. Never activate partial legacy RemoteControlGrant code to “test the UX” on production.
