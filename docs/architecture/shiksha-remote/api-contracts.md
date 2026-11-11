# G. REST and control-plane API contracts v1

Proposed prefix `/api/sessions/collaboration/v1/`, served by existing Django. No new join URL/model. All routes below are new unless called legacy. Browser clients use existing `apiClient`; new device routes accept device-scoped DPoP authentication only, never user cookies as substitute. Exact auth is mandatory per row.

## Shared types and rules

`SessionRef = {kind:'group'|'private'|'live'|'skill', id:uuid}`. Only group enabled initially. `Context` is server-derived `{context:string,profile_id:string|null,auth_session_id:string}`. `B` = authenticated browser cookie + active auth context; unsafe methods require explicit Django CSRF validation and exact allowlisted Origin. `D` = device access token + RFC9449 proof. `P` = pairing secret + original key proof (only pairing poll/finalize). No open redirect; response `Cache-Control: no-store`.

All mutation bodies reject unknown fields. IDs strings; user UUIDs never ints. All non-heartbeat mutations require UUID `Idempotency-Key`; same actor+route+key+same body returns original result, changed body409. Retain24h; auth/expiry checked before replaying sensitive token responses; expired credentials require a new authorized request. Actions on a resource also carry `expected_version`; stale409. GET has no side effects. Return 201 created, 200 updated/read, 204 deleted/revoked device. Error `{code,detail,request_id,field_errors?}`. Standard errors:400 invalid,401 unauthenticated,403 forbidden/CSRF/context,404 nonexistent OR inaccessible resource,409 conflict/version/busy/upgrade_required,410 expired,413 too_large,422 unsupported_capability,429 rate_limited with Retry-After,503 dependency_unavailable. Never infer authority from role sent in body.

Common `Grant` response: `{id:public_uuid,version,state,session,controller:{user_id,display_name},target:{user_id,display_name},device:{id,name},capabilities,expires_at,request_expires_at,reason,epoch,capture_generation,track_sid}` with unavailable fields null before activation. Do not return device credential, signed activation, lease or other participant context secrets to room viewers. Controller/target may read own grant; authorized moderator can only terminate, not obtain native ticket. `Transfer` response is the fields in file architecture without storage_key/raw credentials.

GET `csrf/` (B, exact Origin allowlist) returns `{csrf_token}` from Django `get_token` and sets/refreshes the CSRF cookie; safe bootstrap only, rate30/min/user. The browser sends this masked token in `X-CSRFToken` on unsafe cookie-auth requests. It does not grant API access or disable CSRF checking. Native DPoP routes do not accept cookie credentials.

## Pair/device APIs

| Method/path | Auth and authorization | Exact request fields | Success JSON | Extra failures / limits |
|---|---|---|---|---|
| POST `pairings/` | anonymous DPoP proof of new key; no account assigned | `{name,platform,arch,app_version,public_jwk}` | 201 `{pairing_id,device_secret,user_code,expires_at,poll_interval_s:5}` | 3/hour/IP, 3/hour/key, code collision retry |
| POST `pairings/confirm/` | B, recent account auth ≤10min | `{user_code}` | `{pairing_id,device_name,platform,key_fingerprint,verification_phrase,state:'browser_confirmed'}` | 5 wrong attempts/challenge then invalidate; 10/hour/account |
| POST `pairings/{id}/poll/` | P (secret in Authorization, not URL/body logs) | `{}` | `{state,account_display_name?,verification_phrase?,expires_at}` | one poll/5s; 410 expired; revealing account only after matching secret/key |
| POST `pairings/{id}/finalize/` | P, native physical confirmation | `{local_confirm:true}` | `{device_id,access_token,refresh_token,expires_in:300}` | one atomic redemption; no browser finalize |
| POST `device-token/` | refresh credential + key DPoP | `{refresh_token}` | `{access_token,refresh_token,expires_in:300}` | rotate/reuse revoke; 6/min/device |
| GET `devices/` | B owner/context | none | `{devices:[{id,name,platform,app_version,online,last_seen_at,compatible}]}` | 30/min/account; no global device discovery |
| DELETE `devices/{id}/` | B owner, recent auth | no body | 204; revoke bindings/grants first | idempotent; 10/hour/account |
| POST `device-heartbeat/` | D paired, not revoked | `{app_version,protocol_min,protocol_max,status:'idle'|'sharing'}` | `{server_time,minimum_version,disabled}` | 1/10s idle; must never renew a control grant implicitly |

## Participant and capture APIs

| Method/path | Auth / authorization | Request | Response | Failure/limits |
|---|---|---|---|---|
| POST `bindings/` | B authorized interactive room participant; server RoomService verifies identity/SID | `{session,participant_sid}` | 201 `{binding_id,identity,participant_sid,expires_in:6,capabilities}` | server derives identity from existing token policy, not body; SID mismatch403; 10/min/user |
| POST `bindings/{id}/heartbeat/` | B same auth sid/context/browser binding | `{participant_sid}` | `{expires_in:6,server_time}` | every2s; SID change409, membership stale503; no input payload |
| DELETE `bindings/{id}/` | B owner binding | no body | 204; terminal grants referencing binding | close on page leave; lost request covered by TTL |
| POST `host-bindings/` | B owner of paired device + valid browser binding | `{binding_id,device_id}` | 201 `{host_binding_id,state:'awaiting_local_share',expires_at}` | same user/context; max1/device; 5/min/user |
| GET `host-bindings/{id}/` | B owning host/eligible requester with opaque ID in authorized room snapshot, or D bound device | none | `{id,state,owner:{id,name},device:{id,name},capture:descriptor|null}` | requester sees descriptor only after target exposes availability; no secrets |
| POST `host-bindings/{id}/share/` | D correct device, native display selection confirmed | `{display:descriptor_without_track_sid,local_confirm:true}` | `{livekit_url,token,identity,join_expires_in:60}` | duplicate doesn't widen scope; local capture only; screen_only permission |
| POST `host-bindings/{id}/ready/` | D correct device | `{participant_sid,track_sid,capture_generation}` | `{state:'ready',capture:descriptor}` | server verifies actual native identity/SID/screen track; 409 mismatch |
| POST `host-bindings/{id}/heartbeat/` | D bound device, local sharing still active | `{participant_sid,track_sid,capture_generation}` | `{expires_in:6}` | every2s; descriptor/SID change closes binding |
| DELETE `host-bindings/{id}/` | B owner or D device | no body | 204; terminate grants and stop capture | control UI unavailable when closed |
| POST `ws-ticket/` | B with binding_id or D with host_binding_id; not both | `{binding_id?:uuid,host_binding_id?:uuid}` | `{ticket,expires_in:30,ws_url:'wss://api.shikshacom.com/ws/collaboration/v1/'}` | one-use; 10/min/principal |
| GET `snapshot/?binding_id=<uuid>` | B owner binding | none | `{server_time,host_bindings:[public availability],grants:[own],transfers:[own]}` | 30/min/binding; excludes strangers' private requests |

Deleting a host binding is the API stop-sharing action. Companion may stop locally first and notify later. No browser API can cause unattended capture. `share` display descriptor reports native selection; it is not a command from a browser. Sharing is never enabled simply by polling or listing devices.

## Control grants

| Method/path | Auth / authorization | Request | Response | Failure/limits |
|---|---|---|---|---|
| POST `grants/` | B controller, verified binding, target ready in same room | `{controller_binding_id,host_binding_id,capabilities}` | 201 `Grant` state requested | no client controller/target IDs; busy429/409; 3/min/controller and target; max1 pending pair |
| GET `grants/{id}/` | B involved or D device | none | `Grant`; D additionally gets short-lived consent_nonce when approved | 30/min/B, 60/min/D; 404 strangers |
| POST `grants/{id}/decision/` | B target exact host auth context | `{allow:boolean,capabilities:array,expected_version}` | `Grant` approved or declined | capabilities subset only; lock+unique reserve; no ACTIVE response |
| POST `grants/{id}/activate/` | D exact device, local physical approval latch | `{consent_nonce,expected_version,capture_generation,local_confirm:true,lease_nonce}` | `{grant:Grant,activation_jwt,lease_jwt}` | nonce consumed, membership fresh, ≤30s approval; lease_nonce echoed in signed lease |
| POST `grants/{id}/lease/` | D exact active device/epoch | `{epoch,expected_version,nonce}` | `{lease_jwt}` | 1/s target, max2/s; nonce never extends old reply; not idempotency-cached like mutations; 409 nonactive |
| POST `grants/{id}/pause/` | B controller/target or D device | `{expected_version,reason}` | `Grant` paused | local gate closed before native HTTP; repeat same state idempotent |
| POST `grants/{id}/resume-intent/` | B target with same current binding | `{expected_version}` | `Grant` paused plus fresh intent recorded; D fetches new consent_nonce | no browser-only resume; intent expires30s |
| POST `grants/{id}/resume/` | D device after native physical reapproval | `{consent_nonce,expected_version,capture_generation,local_confirm:true,lease_nonce}` | `{grant,activation_jwt,lease_jwt}` | grant not expired, pause≤60s, fresh epoch; never reconnect |
| POST `grants/{id}/terminate/` | B controller/target; authorized room moderator terminate only; or D device | `{expected_version,reason:'user_stop'|'completed'|'local_stop'|'disconnected'|'capture_lost'|'protocol_error'|'session_ended'}` | terminal `Grant` | terminal stop idempotent; if version stale still revoke current resource after auth (stop wins) |

Server lease endpoint reads current DB/device/state plus fresh bindings; a worker cannot extend a pre-revoke cached authorization. Per-device serialization locks prevent a lease from committing after revoke. Leases minted just before revoke may remain valid for bounded TTL. Use a new random `lease_nonce` in activate/resume requests and local monotonic request deadline exactly as for renewal.

## File transfer APIs

| Method/path | Auth / authorization | Request | Response | Failure/limits |
|---|---|---|---|---|
| POST `transfers/` | B sender currently interactive; recipient bound same room | `{sender_binding_id,recipient_binding_id,name,size_bytes,mime,sha256,mode:'realtime'|'durable'}` | 201 `Transfer` offered, expires60s | name≤255 bytes after sanitization; realtime≤10MiB; durable flag; 5/min/sender and recipient |
| GET `transfers/{id}/` | B sender/recipient only | none | `Transfer` | no implicit host/admin read |
| POST `transfers/{id}/decision/` | B exact recipient/current context | `{accept:boolean,expected_version}` | accepted/rejected `Transfer` | 409 terminal/stale; no file bytes before accepted |
| POST `transfers/{id}/start/` | B sender, recipient still connected/accepted | `{expected_version}` | `{transfer,stream_id,topic,destination_identity,attributes}` or durable upload lease | reserve concurrent slot/quota; stream_id=transfer UUID for attempt1; one-use start |
| POST `transfers/{id}/receipt/` | B recipient | `{expected_version,bytes_received,sha256,result:'verified'|'failed',reason?}` | received/failed `Transfer` | verify expected size/hash; sender cannot declare success |
| POST `transfers/{id}/cancel/` | B sender or recipient | `{reason}` | cancelled `Transfer` | idempotent; invalidate upload/download leases; lifecycle metadata only |
| POST `transfers/{id}/upload-parts/` | B durable sender, accepted/uploading, valid reservation | `{part_numbers:[int]}` | `{parts:[{number,url,expires_at}],upload_expires_at}` | max20 URLs/request, bounded part size/count; scopes exact key |
| POST `transfers/{id}/upload-complete/` | B durable sender | `{parts:[{number,etag}],expected_version}` | `{transfer,state:'scanning'}` | server HEAD/stream hash actual file; untrusted ETag not SHA256; idempotent async scan |
| GET `transfers/{id}/download/` | B authorized recipient, ready, unexpired | none | authenticated attachment stream via internal-media, or 302 short signed exact-object URL | scanner clean + ACL on **every** request; no inline HTML/SVG; no content before ready |

Download signed URL lifetime≤60s is an explicit residual capability window; prefer authenticated Nginx proxy if immediate access revocation is required. No claim that revoking DB can recall an already downloaded file. Durable routes disabled until RC-019.

## Representative calls

Request (controller/target identities derived server-side):
```json
{"controller_binding_id":"00000000-0000-4000-8000-000000000010","host_binding_id":"00000000-0000-4000-8000-000000000011","capabilities":["pointer.move","pointer.button","pointer.scroll","keyboard.physical","keyboard.text"]}
```
Approval:
```json
{"allow":true,"capabilities":["pointer.move","pointer.button","pointer.scroll"],"expected_version":1}
```
Error:
```json
{"code":"capture_not_ready","detail":"The owner must select and share a display in the companion.","request_id":"00000000-0000-4000-8000-000000000012"}
```

## Required API test pattern

Every route above: valid actor, anonymous, unrelated account, same account wrong context, revoked auth/device, malformed fields, expired resource, concurrent duplicate, idempotency mismatch, allowed/forbidden Origin, CSRF and rate limit. Applicable rows also require cross-room/SID mismatch and platform capability tests. Use PostgreSQL for locking/constraint tests; SQLite is insufficient. Tests must verify zero file contents before consent and zero OS authorization on browser decision. Exact serializers/OpenAPI generated in RC-002 are checked against these examples; freeze them before parallel implementation.

## Field precision and readiness rules

Names/device labels max80 characters; OS enum windows/macos/linux, arch enum x64/arm64. Version strings max32 characters, not shell arguments. Display dimensions positive integers≤32768; pixel area≤268435456; rotation enum0/90/180/270; scale finite0.25..8; desktop x/y signed32-bit, bounds widths positive. `display_id` opaque string≤128 characters. Every native descriptor change needs a new generation/binding; do not accept geometry from controller request.

Browser IDs bind exact current auth sid/context and verified participant SID; `bindings/` is additive registration after the existing join, not a new login. Missing legacy auth sid means collaboration requires a normal login refresh/re-auth that supplies a tracked session; it never degrades to account-only authority. `host_binding` phases are awaiting_local_share, joining, ready, closed/failed; readiness deadline30s, renewed TTL6s after ready. Failed/closed bindings cannot restart, so create a fresh binding after local approval. Capture mutation routes require one-use local intent tracked by the trusted companion.

`capabilities` are sorted unique names from the five-item allowlist; nonempty for a request, subset on approval. pointer.button and pointer.scroll each require pointer.move in the approved set; reject inconsistent subsets422. `decision` with allow=false accepts capabilities=[]; allow=true must leave at least one capability. Resume-intent increments version and creates a single-use consent_nonce hash plus intent expiry; it never extends hard expiry. Native lease may include only the current version and epoch.

`Transfer` JSON: `{id,version,session,sender:{user_id,display_name},recipient:{user_id,display_name},mode,state,name,mime,size_bytes,sha256,offer_expires_at,accepted_at,expires_at,completed_at,reason,stream_id}`. Pre-transition timestamps/stream_id are null. Durable adds `scan_state` and `download_available:boolean`, never storage_key or upload secrets. Start realtime sets state=sending and returns exactly `{transfer,stream_id,topic:'shiksha.file.bytes.v1',destination_identity,attributes}`. Start durable sets uploading and returns `{transfer,upload_id,part_size_bytes:8388608,upload_expires_at}` with a one-hour upload deadline; use upload-parts for URLs. Total upload bytes/parts must match declared length (last part smaller allowed).

Grant/snapshot APIs restrict full metadata to involved principals. Snapshot host availability exposes only host_binding_id, owner name/UUID, device label, ready flag and capabilities until controller is allowed to request; descriptor is fetched only for that ready host in the same authorized interactive room. Request mute applies to offers/grants at the server. Add PATCH `host-bindings/{id}/preferences/` (B exact owner or D bound device) with `{allow_requests:boolean}` returning `{host_binding_id,allow_requests}`; defaultfalse until owner enables assistance. This cannot enable capture/control. Rate10/min/owner; CSRF/idempotency as normal. New requests fail403 `requests_disabled` if false; setting false cancels pending requests and leaves active control to explicit Stop.
