# G. Database and migration specification

All new tables live in existing `sessions_app`. UUID user FKs use `settings.AUTH_USER_MODEL`; no role column is invented. PostgreSQL is authoritative for state/revocation. Redis holds expendable liveness, nonce and notification data; Redis loss ends control rather than recreating authority. Fields not explicitly nullable are NOT NULL. UTC timestamps use DateTimeField; JSON values validated against versioned schemas.

## Existing RemoteControlGrant: extend, do not duplicate

Preserve integer primary key and old `session` GroupSession FK/API lookup. Add UUID `public_id` unique for v1 wire use; integer IDs never substitute for UUID user identities. Keep `controller`, `target`, `requested_at`, `granted_at`, `ended_at`, `ended_by`. Widen status to varchar(16), choices per protocol. New fields:

| Field | Type / rules |
|---|---|
| schema_version | smallint default 0 for migrated rows; new writes explicitly 1 |
| session | existing GroupSession FK, nullable for non-group kinds; PROTECT for v1 lifecycle policy |
| private_session / live_session / skill_session | nullable FK to existing PrivateSession / livestream.LiveSession / skills.SkillSession; PROTECT |
| device | nullable PairedDevice FK for legacy; required v1; PROTECT |
| controller_context / target_context | JSON `{context,profile_id,auth_session_id}` derived from verified auth; IDs represented as strings; never body-authoritative |
| controller_binding_id / host_binding_id | UUID; required v1; companion binding points to expiring Redis record and immutable snapshot below |
| room_name, controller_identity, host_identity, companion_identity | varchar(255), server-generated snapshots |
| controller_sid, host_sid, companion_sid, track_sid | varchar(128); required for active v1 |
| capabilities | JSON array from fixed allowlist, sorted unique; no defaults granting input |
| display_descriptor | JSON per realtime contract; required v1 activation |
| capture_generation | positive int |
| epoch | UUID nullable until activation; replaced on pause/resume |
| version | positive bigint default 1; increment every transition |
| request_expires_at / approval_expires_at | datetime; request +60s; approval ≤30s |
| expires_at / paused_until | nullable datetime before relevant state; indexed |
| consent_nonce_hash | binary(32), nullable; one-use activation/resume challenge |
| resume_intent_expires_at | nullable datetime; now+30s after host intent, never beyond paused_until |
| reason | varchar(64) enum from protocol failures |

Constraints: for all rows exactly one of the four session FKs is nonnull; legacy rows keep group session. For schema_version=1 require device/bindings/context fields, controller≠target account, allowed capabilities and valid status/times. PostgreSQL partial unique **device WHERE schema_version=1 AND status IN ('approved','active','paused')**. Partial unique (controller,device) WHERE status='requested' AND schema_version=1 prevents pending-request floods; expired rows transitioned under lock before inserting. Lock device first, grant second consistently. No dependence on nondeterministic NOW() in a partial index.

Indexes: public_id unique, (device,status), (controller,requested_at DESC), (target,status,requested_at DESC), each session FK/status, (status,expires_at), (status,request_expires_at). Audit transitions in same transaction. End/revoke mutates DB even if event push fails; lease reads source of truth. Cleanup every minute is housekeeping, never the authorization clock.

## New PairedDevice

`id UUID PK`, `owner FK User PROTECT`, `paired_context JSON`, `name varchar(80)`, `platform varchar(32)`, `arch varchar(16)`, `public_jwk JSON`, `key_thumbprint varchar(64) UNIQUE`, `credential_hash binary(32)`, `previous_credential_hash binary(32) nullable` (reuse detection only), `credential_expires_at datetime`, `credential_version bigint`, `status varchar(12)` pending/paired/revoked, `protocol_min smallint`, `protocol_max smallint`, `app_version varchar(32)`, `created_at`, `paired_at nullable`, `revoked_at nullable`, `last_seen_at nullable`. Name is escaped display metadata, never authority. Device key re-pairing requires fresh local/browser confirmation; cannot transfer owners in-place. Max five paired devices/account initially. Index (owner,status), credential expiry. Online status uses Redis, not this coarse last_seen_at (persist at most once/5min).

Ownership: only paired owner/current authorized context lists/unlinks. A different learner context on a shared account must create a new local-confirmed host binding; prior context grants never carry over. Device scope never bypasses user's live access. Revoked device cannot refresh, connect WSS, publish new capture or receive a lease. Retain revoked metadata 90 days; clear credential material immediately. Account deletion revokes first, then follows site's deletion/anonymization policy; no uncontrolled cascading during active input.

## New FileTransfer (metadata plus optional durable asset)

No new SharedFile table in MVP. `id UUID PK`; four nullable session FKs with exactly-one constraint; `sender FK User PROTECT`, `recipient FK User PROTECT` and distinct; `sender_context/recipient_context JSON`; `sender_binding_id/recipient_binding_id UUID`; `sender_identity/recipient_identity varchar(255)`; SID snapshots varchar(128); `mode varchar(12)` realtime/durable; `state varchar(16)` per file architecture; `version bigint default1`; `name varchar(255)` sanitized; `declared_mime varchar(120)` informational; `size_bytes positive bigint`; `sha256 char(64)` lowercase; `stream_id UUID nullable UNIQUE`; `attempt int default1`; `created_at`, `offer_expires_at`, `accepted_at nullable`, `expires_at`, `completed_at nullable`, `reason varchar(64)`.

Durable-only extension in later migration: `storage_key varchar(512) UNIQUE nullable` (opaque private object key), `upload_id varchar(255) nullable`, `scan_state varchar(12)` pending/clean/rejected/error, `detected_mime varchar(120)`, `actual_size bigint nullable`, `actual_sha256 char(64) nullable`, `reserved_bytes bigint default0`, `scan_completed_at nullable`. No database blob. One row authorizes one recipient; course-wide distribution continues existing course models/ACLs, never overloads recipient=null to mean “everyone”.

Constraints: mode/state compatibility; size 0..configured maximum at service validation; received requires integrity match; durable ready requires clean scan and verified size/hash. Index sender/state/created_at, recipient/state/created_at, expires_at/state. Quota reservations use row locks on user or an existing quota owner; storage bytes reserved before multipart URLs. Default quota proposal 1GiB stored/account, durable max100MiB/file; not a billing promise.

## New CollaborationAuditEvent

`id UUID PK`, `occurred_at datetime`, `actor_user nullable FK User SET_NULL`, `actor_device nullable FK PairedDevice SET_NULL`, `actor_context JSON` limited identifiers, `event_type varchar(64)`, `resource_type varchar(24)`, `resource_id varchar(64)`, `resource_version bigint`, `session_kind varchar(16)`, `session_id UUID nullable`, `reason varchar(64)`, `metadata JSON` allowlisted counts/capabilities/app version only. Unique (resource_type,resource_id,resource_version,event_type); indexes time, resource/time, actor/time. Append-only service API and restricted DB permissions; not a tamper-proof log against DB administrators. Retain 90 days by default, subject to explicit product/privacy review. No keystrokes, text, screen frames, file contents, bearer tokens, DPoP secrets or filenames in audit metadata.

## Ephemeral Redis records (not extra meeting models)

- Pairing: challenge UUID, key thumbprint, code/secret hashes, expiry, approved user/context, attempts and finalization state; TTL5min, atomic consume.
- Browser binding: binding UUID, SessionRef, user/context/auth sid, identity/SID, last heartbeat and last server membership verification; TTL6s. Attach secrets never returned to another user.
- Host binding: UUID, device/user/context/session, owner browser binding, local share consent, companion identity/SID, capture descriptor, last heartbeat, phase, allow_requests flag, pending local-share intent deadline; TTL6s while sharing; max one live host binding/device enforced under device lock.
- DPoP nonce/replay cache, single-use WSS tickets TTL30s, per-binding idempotency cache TTL24h, rate buckets, targeted notification groups. Redis flush → require reconnect/reconsent. Do not synthesize bindings from old SQL rows.

## Migration sequence and rollback

1. RC-001 checks actual migration graph; do not assume next migration number. Existing 0015 already created Grant, SessionFile and GroupSessionParticipant; 0017 added PrivateSessionFile.
2. Add nullable columns/tables and public_id initially nullable. Backfill public IDs per row in batches; expire all nonterminal schema_version=0 control rows, reason legacy_migration. Never convert old approval into native consent.
3. Add check/index constraints after verification; add UNIQUE indexes concurrently for large tables via SeparateDatabaseAndState/atomic=False as appropriate, reviewed by migration owner. Small tables still need measured lock plan.
4. Deploy compatibility code reading old rows but creating only v1 through feature-gated new routes. Legacy request/respond routes return stable 409 `upgrade_required` for control rather than granting v1; legacy revoke can terminate matching legacy rows and same-user v1 group rows only after auth.
5. Flip flag only for certified clients/allowlisted users. Application rollback turns off flag, revokes grants, waits leases out and rolls back code; leave additive tables/columns. Do not drop data or restore old native authorization paths. Production migration application requires separate reviewed release procedure.

Retention: terminal grants/transfer metadata 90d, rejected/expired pending offers 7d, temporary file bytes immediately on failure and at latest24h, durable bytes default7d or lower policy, audit90d. Every download checks expiry even if sweeper is late. Existing room-file retention stays configured via live_rules until its separately reviewed hardening patch.

## Lease serialization detail

Redis stores per-active-grant lease sequence and consumed request nonces keyed by epoch; a cache miss for an established epoch terminates authority instead of restarting the counter. DB device/grant locks serialize lease issuance against revoke; construct/sign the bounded lease while holding the lock, then release. A delayed HTTP response cannot reset its request-start deadline. Refresh-token rotation/reuse state is serialized on PairedDevice. Atomic idempotency records include authenticated principal, route and canonical request-body hash, not just a user-supplied key.
