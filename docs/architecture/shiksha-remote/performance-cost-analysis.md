# K. Performance, capacity and cost

No benchmarks or Shiksha billing data were measured. Estimates below are transparent planning scenarios. 1,000 LMS accounts is not 1,000 simultaneous media connections.

## Planning scenarios

| Scenario | Human concurrent connections | Companion connections | Simultaneous native shares/control pairs |
|---|---:|---:|---:|
| Pilot | 20 (10 pairs) | 5 | 5 |
| Initial busy hour assumption | 100 (20 rooms ×5 people) | 20 | 20 |
| Stress target, not forecast | 300 | 75 | 75 |

Companion subscribes to no room media. A room viewer receiving native screen video consumes downstream bandwidth; five-person room can have four remote viewers plus the host browser preview. Limit host preview to a small/disabled tile by default after handoff; counting that subscription still matters. Stop duplicate browser capture after native handoff. Native screen publishing does not double ordinary audio/video participants, but adds a connection.

Input60 moves/s ×300 illustrative JSON bytes ≈18KB/s (144kbps) before transport overhead; reliability/heartbeats/actions add traffic. Measure actual packet histogram. Django receives **no pointer/keyboard event writes**. At20 controls, device leases≈20 requests/s; browser bindings≈20 requests/s for40 browsers; host-binding heartbeats≈10/s; coalesced room membership polls≤10/s for20 rooms at2s cadence. Additional idle device/other meeting browser heartbeats add load. At75 controls these numbers scale roughly3.75×. Start with existing ASGI/Redis/Postgres; benchmark DB reads, lock contention and LiveKit management API limits before adding infrastructure.

Current lease design rechecks PostgreSQL state each second; no SQL write per lease. Lease counters/nonces are Redis. Audit only lifecycle transitions. Preserve atomic revoke ordering. If measured database/API overhead is unacceptable, architecture owner may approve revocation-aware caching with an equally bounded fail-closed proof; agents must not independently cache authorization for minutes.

## Screen and latency budgets (targets)

Start1080p at15fps, adaptive max30fps for motion,2–4Mbps illustrative screen bitrate. Real content/codec changes bitrate substantially; native display4K need not always encode4K. Readable code at 100%/1:1 zoom is a acceptance artifact, not inferred from nominal resolution. Prefer codec supported by browser+Rust+OS with measured hardware encoding; do not assume Rust SDK advertises a codec means selected hardware path exists.

Measure separately: controller event→native dispatch, dispatch→OS visual change, capture→render, end-to-end pointer/key visual feedback. Targets on named same-region broadband: p95 event→inject<150ms, p95 visual feedback<300ms, p95 local Stop gate<50ms, no queued event after gate closure. These are release targets, not LiveKit SLA or universal internet claims. High RTT/loss must pause safely and keep normal meeting alive. Use synthetic secrets in latency/input tests; never record real keystrokes.

## Cost calculation and source snapshot

[LiveKit pricing](https://livekit.com/pricing), accessed2026-10-12, lists Build $0/month, Ship $50/month, Scale $500/month. Ship includes150,000 WebRTC connection minutes then$0.0005/min and250GB downstream then$0.12/GB; Scale includes1.5M minutes then$0.0004/min and3TB downstream then$0.10/GB. These are transport line items, not AI agent-session fees. Actual account contract, region, taxes and future prices must be verified before purchase. Plan concurrency limits also matter; snapshot lists100/1,000/5,000 connections for Build/Ship/Scale.

Illustration:20 simultaneous controls ×60min/day ×22days =26,400 companion connection-minutes/month. At a fully exhausted Ship inclusion, incremental connection charge≈$13.20. At3Mbps, one receiving subscriber consumes3×3600/8/1000=1.35 decimal GB/hour.20 shares ×22hours ×1 receiver =594GB/month; four receivers=2,376GB/month. Screen downstream overage upper marginal rate at$0.12/GB is$71.28 or$285.12 if all included GB are already consumed. Native capture **replaces** browser share, so much of this screen traffic already exists; do not attribute the whole screen bill as incremental assistance cost. Native connection minutes are incremental. Include extra preview/recorder subscriptions when applicable.

Pure scenario using a fresh Ship allowance and only two browser participants+one companion for those20 pairs gives79,200 total connection minutes (within150k),594GB screen-only downstream, and$50+max(594−250,0)×$0.12=$91.28/month **before normal audio/video, files, recording, infrastructure and tax**. This is an example, not a Shiksha quote. Real baseline livestream use may consume allowances first.

File egress:500 approved transfers/day ×5MiB ×22days≈53.7GiB/month, separately billed according to actual transport/storage usage. Durable storage cost formula = average retained GB×storage rate + uploaded/downloaded GB×provider transfer rate + requests + scanner compute; no provider quote was verified, so no invented dollar estimate. Retention7days reduces stored average relative to cumulative monthly uploads. Backend signing/CI/macOS signing and Windows certificate costs also need real quotes.

## Measurement/reliability plan

Observe p50/p95/p99 latency, loss, dropped/coalesced events, lease expiry reason, reconnect count, scanner delay, scan failure, successful integrity receipts, quota cleanup lag and signing version. Aggregate counts only. Record CPU/RAM/GPU/installer measurements in RC-003/028 with hardware, OS and exact dependencies. See native budgets in companion design.

Test UDP-blocked/TURN/TLS-only networks, enterprise proxy,150/300/800ms RTT,1/5/10% loss, bandwidth1/3/10Mbps, mobile hotspot and sudden offline. LiveKit handles NAT traversal/relay; no new inbound ports. TLS/TURN restrictions can still make a network unsupported. Device WSS/HTTPS unavailable or backend overload closes control after lease expiry. Reconnection restores normal meeting independently; the user must reauthorize assistance.
