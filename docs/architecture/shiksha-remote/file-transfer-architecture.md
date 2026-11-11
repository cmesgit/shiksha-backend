# F. Secure file collaboration

Existing `SessionFile`, `PrivateSessionFile`, files APIs and FilesPanel already support durable room-scoped uploads. Preserve that tab as **Room files**. Add **Send privately** within the panel. File-transfer authorization is independent of remote control; any eligible interactive participant can offer another a file without an enrollment relationship in an instant meeting. Broadcast audience cannot spam offers.

## Realtime private transfer

MVP configurable limits: 10MiB/file, two active transfers per user (sending + receiving combined), five per room, 50MiB total pending receive allocation per browser, offers expire60s, accepted/start deadline60s, sending timeout5min and idle timeout15s. Concurrent limits are atomically reserved server-side. These are product defaults, not LiveKit hard limits. Sender hashes selected file with SHA-256 in a worker before offer. At10MiB, bounded buffering is acceptable; do not call unbounded readAll on declared arbitrary lengths.

State machine: `offered → accepted → sending → received`; `offered → rejected`; any nonterminal→`cancelled|expired|failed`. Only recipient verifies final digest/size and supplies receipt; sender progress100% means “sent; waiting for verification”, not success. Data delivered after cancellation is discarded. Late receipts never reactivate a cancelled transfer. Reconnect/browser refresh fails the attempt; manual retry creates a new offer/consent/transfer ID, never appends unverified bytes. LiveKit transport recovery must not be presented as durable resumable storage.

```mermaid
sequenceDiagram
  participant S as Sender
  participant D as Existing Django
  participant R as Recipient
  S->>S: Select file; compute size and SHA-256
  S->>D: Offer metadata to one recipient
  D-->>R: Private offer notification
  R->>D: Accept
  S->>D: Start accepted transfer
  D-->>S: Bound stream ID + exact recipient identity
  S->>R: LiveKit targeted byte stream
  R->>R: Bounded receive; validate bytes + SHA-256
  R->>D: Verified receipt
  D-->>S: Received status
  R->>R: Explicit Save download
```

Nothing calls sendFile/streamBytes/file upload before required recipient acceptance. Reading/hashing locally is permitted. Stream header must match authorized transfer ID, sender identity and current participant SID, recipient identity/SID, exact size/digest, mode/topic and accepted state. Attributes are `{v:'1',transfer_id:uuid,sha256:hex,attempt:'1'}`. They are untrusted hints until compared to authenticated API metadata. Set `streamId` to returned transfer stream_id and `destinationIdentities:[recipient_identity]`. Reject missing/empty destination in wrapper. No content/filename broadcast through chat or room metadata.

Local client version2.17.2 contains sendFile and streamBytes. Its sendFile is a convenience loop with no exposed application cancellation handle and does not copy all desired metadata. Use `streamBytes` + explicit selected File reader loop, bounded chunks (application chunk≤64KiB, SDK packetizes), awaiting each write and checking cancellation before the next read/write. The installed writer exposes `close`, not a documented `abort` method. On cancel stop producing, attempt close after in-flight write resolves, mark server transfer cancelled and reject truncated completion at receiver. Set receiver AbortSignal/use iterator support as verified in RC-017; discard buffers and Blob URLs. Do not disconnect the whole LiveKit room to cancel a file.

A write may already be in the SCTP buffer when cancellation happens; bounded chunks/backpressure limit it, and receiver rejects it. To prevent files delaying control on a shared reliable data channel, disallow starting realtime streams while any control grant for these participants is active/approved; defer file transfer or use durable path. At start of control, cancel/drain their ongoing realtime streams and require control-plane freshness before opening gate. This is preferable to promising priority separation the current SDK may not supply. Future measurement may safely relax it.

Unsolicited/malicious LiveKit participants can still send raw data if their existing room token permits it. The application receiver enforces accept-before-allocation and immediately discards unauthorized streams; this cannot prevent an attacker spending network bandwidth at the SFU. Apply LiveKit participant moderation/rate monitoring and disconnect abusive senders through existing room administration. Do not claim the Django offer gate is a network firewall for arbitrary modified clients.

## Safe receive/download

Treat MIME, name, extension, hash and sender as untrusted. Normalize basename; remove path separators, NUL/control characters, directional spoofing characters; cap255 UTF-8 bytes, escape UI, show actual extension. Hash is integrity against advertised content, not malware detection. Show claimed MIME as unverified; warn for executable/macro/archive types. No auto-open, preview script execution, unzip, overwrite or native file path. After verification create bounded Blob, explicit Save link with download attribute, revoke URL after use/cancel. Browser controls final save location. HTML/SVG downloads must not be opened in an app-origin preview; use attachment semantics for server downloads and a no-script isolated preview only if separately reviewed.

Realtime bytes are not server-scanned. UI says “Not malware scanned; only open files from someone you trust.” If scanning is mandatory for an organization or blocked file type, require durable quarantine transfer; do not claim SHA-256 makes content safe. Initial realtime denylist includes executables/scripts/installers (`exe,msi,bat,cmd,ps1,sh,app,dmg,apk`) and macro-enabled Office formats; code source such as `.py` is permitted as a download with explicit unscanned warning, never execution. MIME/magic ambiguity routes to durable scan or rejection. Archive bombs tested on durable scanner, never unpacked client-side.

## Durable transfer (later phase)

Reuse existing private-storage abstraction/Nginx authorization patterns; existing Bunny CMS configuration is **not proof of private recipient-safe object storage**. RC-019 selects a configured private S3-compatible bucket or proves equivalent private access in current provider. No public CDN route for quarantine/recipient files. Object keys are random, not user names. Recipient acceptance precedes content upload here too. Course distribution remains in existing StudyMaterial/room-file flows and retains course/batch ACLs; do not turn a private transfer into room-wide material by changing a recipient field.

`offered → accepted → uploading → scanning → ready → received`; reject/cancel/expire/fail terminal at any point. Server reserves full declared quota before issuing signed multipart URLs. Default100MiB/file,1GiB stored/account,7-day retention; configurable under existing admin policy. Multipart part URLs scoped to exact key/upload/part, expire5min; listing/completion/download remain authenticated. Storage lifecycle aborts incomplete uploads after24h. Enforce declared and actual length, detect MIME server-side, stream SHA-256 server-side, compare with offer; do not treat multipart ETag as digest. Abort on excess bytes or quota race.

Celery scans quarantine objects with pinned/updated malware engine, resource/time limits, archive depth/expanded-size limits, encrypted archive policy=reject. Scanner error/unavailable remains quarantined; no fail-open download. Only verified clean object transitions to ready. Download route checks owner-recipient ACL, context, retention, account status and scan result, serves attachment with safe Content-Disposition and nosniff; logs metadata without filenames. Access may persist after meeting ends for the approved recipient until expiry; new transfers require an active meeting. Sharing with another person creates another explicit authorized offer, not a reusable room URL.

GET can return a≤60s signed download URL with its limited revocation window disclosed, or proxy via existing internal-media path. Revocation cannot erase bytes already saved by a recipient. Delete/expiry stops new access immediately at API, enqueues object deletion, releases quota only after confirmed cleanup, retries idempotently and reconciles orphan objects. Retention sweeps and storage lifecycle are independent safeguards. Native file transfer stays deferred: it would require explicit local picker/save destination, identical consent/scan policies and no arbitrary filesystem API.
