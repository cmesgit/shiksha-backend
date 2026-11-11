# L. Phased implementation roadmap

Dependencies refine the requested phases: perform a minimal native capture/input spike early to validate feasibility before building all UI; finalize shared contracts before parallel producers/consumers. No production activation until end-to-end safety validation. Task IDs are authoritative in [task graph](task-dependency-graph.md).

| Phase | Tasks | Exit gate | Rollback |
|---|---|---|---|
| 0 Audit/baseline/feasibility | RC-001,003,004 | Existing meeting contract tests, runtime plan, native SDK/input/capture evidence | Delete isolated spike; keep documents/tests; no production feature |
| 1 Contracts/backend boundaries | RC-002,005–009 | Frozen fixtures; additive migrations; CSRF/pairing/bindings/state/lease tests | Flag off, revoke rows, leave schema; never enable legacy native authority |
| 2 Browser integration | RC-010,011 | Shared source ownership reconciled; UI states/accessibility with mocked API | Remove feature UI flag; old room flows work |
| 3 Native MVP | RC-012–014 | Pairing, signed grants, local consent, real Windows input/capture and kill cleanup | Unlink/disable device, remove pilot installer |
| 4 End-to-end control | RC-015,016 | Bidirectional user-pair demo, stop/disconnect/replay/race tests | Global kill flag; wait leases; keep meetings live |
| 5 Files | RC-017–019,031 | Recipient consent/integrity/cancel; existing media preserved; optional durable quarantine | Disable new transfer modes; cancel uploads; existing room files remain |
| 6 Session/platform/mobile expansion | RC-020–027 | Individually tested contexts/platforms; explicit compatibility matrix | Disable failing adapter/context only; Windows/group still works |
| 7 Production | RC-028–030 | Signing/updater, security/load review, deployment/runtime reconciliation and rollout drill | Kill flag, signed recovery release if needed, retain additive schema |

Windows/group pilot may ship before all Phase6 adapters **only** after RC-028–030 apply to its smaller certified scope. Cross-platform tasks are not justification for delaying a working safe MVP, and are not claimed completed by Windows success. Durable files likewise optional after realtime MVP. Course/public policies must not be inferred from group behavior.

## Working rules for coding agents

Astra is the user's architecture/security decision owner. Use bounded implementation models for serializers/components/fixtures/adapters after contracts freeze; require higher-capability review for authorization, concurrency, cryptography, input, platform permission and updates. Task prompts avoid tying correctness to a specific model brand/version.

Do not modify existing meeting URL shapes, room names, browser identities, role/enrollment gates, attendance meaning, recording policy or auth token rotation except where a specific reviewed task allows additive integration. Do not touch unrelated shared AuthContext drift. No blanket dependencies upgrade, app rewrite, shell executor, unattended control, clipboard sync or filesystem browser.

Tasks can be independently tested using signed fixtures, fake OS adapter and fake clocks. Fake adapters are never labeled working OS control. Parallel implementation is allowed only **after** RC-002 freezes schemas and only on disjoint files/explicit contracts; this specification stage did not launch coding agents. Resolve changes by the designated contract owner, not by letting consumers accept arbitrary alternatives.

For every phase: review migration plan before production, run applicable existing tests, record before/after results, stage with feature off, and have a concrete rollback. No task done merely because code generated. Security-critical merge requires stronger review and actual evidence.
