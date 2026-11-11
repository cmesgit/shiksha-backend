# Shiksha Remote Assistance — implementation specification

**Status:** proposed architecture, repository-audited; not implemented or platform-certified. **Audit/research date:** 12 October 2026 (Asia/Kolkata). No application code, database, or production configuration was changed for this specification.

Extend the existing Shiksha Live interfaces and rooms with a separately paired Rust companion on the controlled desktop. Controllers use the website. Django owns identities, session policy and consent records; the companion independently owns permission to inject OS input. Existing screen sharing, chat, attendance, recordings, enrollment gates, meeting URLs and join responses remain compatible.

Start with Windows 11, one selected full display, ordinary user applications, and existing instant/group sessions. Support student↔student, teacher↔student and teacher↔teacher through participant authorization, never teacher privilege. Windows 10 is a compatibility-test target, not the first supported release. Subsequent adapters cover macOS, X11 and individually tested Wayland environments. **COSMIC has a merged RemoteDesktop implementation; it must not be described as categorically unsupported.** Installed versions still need testing.

## Decisions that unblock implementation

- Reuse `sessions_app.RemoteControlGrant`; evolve its schema through additive migrations. Do not create another meeting or request table.
- Reuse Channels/Redis and existing authentication; device credentials are subordinate paired-device credentials, not another user login system.
- Companion uses outbound TLS connections to Django and LiveKit. No localhost server, inbound port, extension, unattended service or elevation.
- Companion is a visible associated LiveKit participant in the same room, with screen-only media publishing and no media subscriptions. Input packets target it explicitly.
- For control, select/capture the display in the companion. This provides a verified capture→desktop mapping. Browser tab/window capture remains view-only. Switching capture requires explicit host action and visible scope disclosure.
- Each grant needs browser approval **and a locally operated native consent dialog**. Reconnect terminates control; it never resumes a grant automatically.
- A 3-second server authorization lease and 1-second input freshness deadline limit failure exposure. Local Stop closes the injection gate synchronously. Remote revocation under a partition is bounded, not mathematically instantaneous.
- Keep room-wide files intact. Add recipient-approved private transfers using targeted LiveKit byte streams; durable private storage comes later.

## Deliverables, in requested order

| Deliverable | Authoritative document |
|---|---|
| A Repository audit | [existing-system-audit.md](existing-system-audit.md) |
| B Feasibility | [feasibility-platform-matrix.md](feasibility-platform-matrix.md) |
| C Architecture | [architecture-decisions.md](architecture-decisions.md) |
| D Native companion | [companion-architecture.md](companion-architecture.md) |
| E Control protocol | [remote-control-protocol.md](remote-control-protocol.md), [realtime-event-contracts.md](realtime-event-contracts.md) |
| F Files | [file-transfer-architecture.md](file-transfer-architecture.md) |
| G Database/API | [database-specification.md](database-specification.md), [api-contracts.md](api-contracts.md) |
| H Frontend | [frontend-ux-specification.md](frontend-ux-specification.md) |
| I Threat model | [permission-security-model.md](permission-security-model.md) |
| J Compatibility | [feasibility-platform-matrix.md](feasibility-platform-matrix.md) |
| K Performance/cost | [performance-cost-analysis.md](performance-cost-analysis.md) |
| L Phases | [implementation-roadmap.md](implementation-roadmap.md) |
| M Agent tasks | [task-dependency-graph.md](task-dependency-graph.md), individual prompts under `tasks/` |
| N Validation | [security-test-plan.md](security-test-plan.md) |
| O First executable prompt | [tasks/phase-0/RC-001.md](tasks/phase-0/RC-001.md) |

Deployment and rollback: [deployment-rollout.md](deployment-rollout.md). Primary references: [sources.md](sources.md). Local verification record: [validation-report.md](validation-report.md).

## Engineering governance

Three decisions in this pack are **product/policy/safeguarding calls, not engineering ones**, recorded with named blocking tasks in [architecture-decisions.md](architecture-decisions.md). **OD-01 is resolved** (11 Nov 2026): peer-to-peer control is permitted inside instant collaborations, including un-authenticated lobby guests, and is teacher-initiated only in scheduled and classroom contexts. **OD-02 (audit retention) and OD-03 (unknown recording status) remain open.** An architecture reviewer is not the right owner for these and an implementation task must not settle one by default.

The user's designated architecture owner, Astra, approves critical architecture/security revisions. This document does not claim that an independent Astra review has occurred. Bounded implementers use the approved contracts verbatim; stronger reasoning reviews authorization, concurrency, cryptography, input injection and updates. A reviewer must approve a contract revision before dependent tasks adopt it. No model needs to redesign meeting authentication.

All paths labeled **existing** were inspected. All paths labeled **new** are proposed. Repository paths in this pack are relative to the workspace (`/home/hruaia/workspace/projects/work`), except explicitly backend-relative paths. Documents are stored in the backend repository to be versioned. Do not treat documentation as deployed functionality or treat design targets as measurements.

There are genuine release gates: runtime mismatch in Docker, baseline shared-file drift, incomplete control authorization, exact LiveKit Rust SDK compatibility and platform measurements. Tasks resolve these before rollout. The first task is a small compatibility/authorization contract test patch, not a replacement livestream application.
