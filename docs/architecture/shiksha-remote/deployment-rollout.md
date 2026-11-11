# Deployment and rollout

Reuse existing Nginx + ASGI Django + Redis/Channels + Celery/Postgres + LiveKit Cloud. This checkout uses Uvicorn and a Python3.11 Dockerfile inconsistent with Django6; PostgreSQL image15 differs from supplied16 context. RC-004 records supported/tested actual runtime and proposes small deploy correction; do not “fix” production blindly. Nginx must support existing WS upgrades/timeouts and a new collaboration route; no new public input port.

## Configuration (new unless existing named)

Keep existing GlobalSettings.live_remote_access_enabled as master kill switch. Add structured collaboration settings: enabled_session_kinds initially[]; allowed_pilot_user_ids[]; minimum_companion_version; control_request_ttl60s, approval_ttl30s, grant_ttl600s, lease_ttl3s, renew1s; remote_file_mode flags; realtime_max10MiB, durable_max100MiB; quotas/retention; signing key IDs/trusted public keys; CSRF exact origins. Security ceilings (lease≤3s, no unattended) cannot be extended by routine admin configuration. Schema version1 required. Defaults disabled.

Secrets: separate signing key for control/device tokens, DPoP nonce/HMAC storage keys, existing LiveKit API secret server-only, scanner/private-storage credentials later. Never ship signing private keys/LiveKit API secret to companion/browser. Signing manifests use offline release trust distinct from online grant signing. Versioned public keys packaged with secure rotation procedure.

## Deployment steps

1. Capture baseline commit IDs, current applied migrations, real runtime/DB/cloud plan, feature flags, admin storage/recording policy. Back up DB; verify restore in staging.
2. Review additive migrations and lock time on realistic row counts; expire legacy grants. Deploy backend with all new flags off. Existing API contract/livestream tests and join canary must pass.
3. Deploy browser UI code inactive; publish internally signed companion builds. Verify no companion autojoin/capture at startup or update. Production assets use CSP and trusted origins.
4. Run private staging two-PC test with different user roles plus network-failure tests. Confirm companion is excluded from human attendance/counts yet shown visibly as owner-associated device. Existing webhooks must ignore its prefixed identity before parsing user IDs.
5. Enable allowlist for5 internal users, then25 consenting pilot users, then staged5%/25%/100% of eligible group-session users based on explicit operational review, not an unattended timer. Public/broadcast sessions stay off.
6. Monitor aggregate failure/safety/latency/meeting regressions, signer version, update adoption, unexpected native identity count, stale sessions and transfer cleanup. Disable on any unauthorized input, consent bypass, stuck input after termination or breakage of ordinary joins.
7. Expand session/platform flags only after their own task certifications. Signing and compatibility evidence stored with release, not inferred from upstream support.

## Stop/rollback procedure

Disable master flag and new grants; transactionally revoke all nonterminal grants, publish private notifications, stop lease renewals. Native gates close immediately on received stop or within3s last authorized lease; confirm aggregate lease activity drains. Stop/expire host sharing bindings if disclosure risk requires capture shutdown. Remove companion participants best-effort via existing LiveKit administration; this alone is not the input guarantee. Normal human room connections stay intact.

Roll back frontend/backend to last compatible build after lease drain. Keep additive schema and terminal rows. Never roll back to a build that enables legacy browser-only approvals as native grants. File rollback cancels pending transfers/multipart leases, leaves already accepted durable objects quarantined/subject to existing ACL and cleanup; do not delete unrelated room files. Signed recovery update follows antirollback-authorized manifest, no unsigned downgrade.

## Operational jobs and metrics

Existing Celery worker handles minute cleanup: expire stale requested/approved/paused grants; purge pairing/ticket caches by TTL; terminal control audit retention; file expiry and multipart/orphan reconciliation. Jobs are idempotent. Lease correctness never depends on Celery running on schedule. New WS connections must not increment existing human chat active_connections.

Metrics: counts/reasons for request/deny/start/stop/lease-expire; device version/OS family; coarse input rate/dropped counts, no payloads; p95 native stop/latency; file bytes, cancellations, checksum errors, quarantine age. Alerts for grants stuck active after lease expiry, unknown signing key, rate anomalies and cleanup failures. Audit access is restricted; private file access has no generic staff bypass.

No public deployment or message to other users is part of this documentation task. Real installers, infrastructure quotes and operating-system support remain implementation deliverables.
