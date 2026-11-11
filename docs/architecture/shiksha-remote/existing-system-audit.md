# A. Repository audit

Static inspection of available working trees; no production/cloud account inspection or live room/device test. Backend worktree was clean before these documents. Comments were checked against code where possible; several old comments contradict later hardening.

## Repository inventory

| Existing workspace path | Evidence / snapshot |
|---|---|
| `shiksha-backend` | HEAD `4447bb455d7bb7d102632b7c78b9d6a0213c98d4`; Django, DRF, Channels, Redis, Celery, LiveKit API |
| `shiksha-student-dashboard` | HEAD `57ffbe403f6f645080bd355caae74b2ded398108`; React 19.2, LiveKit client range ^2.17.2 |
| `shiksha-teacher-dashboard` | HEAD `b2d27ca034c6c81c469beb21d0c42d3c2c0c957d`; matching React/LiveKit ranges, Vitest script |
| `shiksha-frontend` | HEAD `153ff2ae9cc53052ee8bd8eac0655ca639d8cefe`; main-site LivePreJoin/GroupSessionLive, partial control entry |
| `shared` | canonical copy-and-sync modules; `shared/sync.mjs` defines actual targets, README is incomplete |
| `shikshacom_app` | Flutter/Dart `pubspec.yaml`, `lib/features/live/*`, `livekit_client: ^2.8.1`; no Git root found for this folder |
| `Admin-dashboard` | separate admin frontend; not a general shared-live sync target |

No applicable AGENTS.md found in workspace searches. No Rust companion/Cargo project or Expo app manifest was found in the inspected project tree. An Expo repository elsewhere remains unverified.

## Verified general meetings: preserve these

`shiksha-backend/sessions_app/group_session_views.py:1775` (`instant_create`) accepts authenticated users subject to existing entitlement policy, creates **GroupSession(session_type='instant', subject=None, invited_teacher=None)** and opens `group_session_<uuid>`. `join_group_session` at line 1132 skips course-invite gates for instant meetings while retaining lobby, duration, capacity and entitlement logic. It does not impose a teacher/student pair. This is the existing general user-to-user meeting, not a missing feature.

Endpoints already exist under `/api/sessions/group-sessions/`: `instant/`, `join-by-code/`, `<uuid>/join/`, lobby admission, end, chat, files, notes, summary and review. Dashboard `GroupSessionLive.jsx` is canonical in `shared/src/pages/`; main-site `shiksha-frontend/src/pages/GroupSessionLive.jsx` is a separate implementation. Keep all URLs and old join-response fields.

## Integration map

| Concern | Existing files/functions | Reuse / caution |
|---|---|---|
| Auth/user identity | `accounts/models.py:25`, `accounts/authentication.py`, `accounts/auth_flow.py`, `accounts/revocation.py` | UUID user + RBAC + learner/teacher context; never cast user IDs to int. Preserve JWT session revocation |
| Group token | `sessions_app/services/group_session_token.py` | Identity `<user_uuid>_<session_uuid>`; 60-minute JWT; room metadata. Preserve browser identity |
| Private sessions | `sessions_app/models.py:6`, `views.py:910`, `services/private_token.py` | PrivateSession and participant checks; identity `<user>_<session>`; retain accepted participant rules |
| Course live sessions | `livestream/models.py:12`, `views.py:join_live_session`, `services/token.py` | Current identity `<user>_<profile-or-x>_<session>`; legacy parser also exists. Viewers may publish **microphone only**, spectators cannot publish data |
| Skill sessions | `skills/livekit_views.py:JoinSessionView` | Additional existing context: `skill-session-<uuid.hex>` and `expert-<user>` / `learner-<user>` identities. Separate profile-sensitive adapter, later stage |
| Presence/lifecycle | `livestream/views.py:livekit_webhook`, `sessions_app/services/group_attendance.py`, `private_attendance.py`, `livestream/tasks.py`, `sessions_app/group_session_tasks.py` | Signature-verified webhook log, attendance reconciliation, end tasks. Do not count companion as learner or open an attendance interval |
| WebSockets | `config/asgi.py`, `accounts/middleware.py`, `sessions_app/consumers.py`, `livestream/consumers.py` | Existing authenticated Channels infrastructure. Group consumer **now** authenticates and checks participation; older model notes saying it is anonymous are stale |
| Shared UI | `shared/sync.mjs`, `shared/src/components/live/GroupSessionControlBar.jsx`, `shared/src/hooks/useLiveSessionChat.js` | Edit canonical files only. GroupSessionClassroomUI and FilesPanel are currently per-app, not all canonical |
| Screen share | dashboard `components/live/GroupSessionClassroomUI.jsx`, shared GroupSessionControlBar | Existing `Track.Source.ScreenShare` focus pane, full screen, normal LiveKit sharing; add a trusted companion-track selector |
| Files | `sessions_app/live_files_views.py`, models `SessionFile`/`PrivateSessionFile`, migration 0015/0017, per-app `FilesPanel.jsx` | Existing room-wide persistent upload/delete/progress/expiry UI. Not private recipient consent or peer transfer |
| Storage authorization | `config/secure_local_storage.py`, `config/media_security.py:_check_session_file`, `config/media_views.py`, `config/tests_media_security.py` | Private authenticated media URL + Nginx internal media. Do not incorrectly call current file URLs public; expiration/scanning/private recipient ACL need work |
| Partial remote control | `sessions_app/remote_control_views.py`, `models.py:940`, migration `0015_groupsession_cap_ends_at_and_more.py` | Extend existing RemoteControlGrant; current code is authorization scaffolding, no native injection |
| Main-site control | `shiksha-frontend/src/api/liveSessionService.js:88`, `components/live/GroupSessionClassroomUI.jsx:671` | API wrappers and disabled/TODO teacher-only entry; not functioning OS control |
| Recording | `courses` SessionRecording references, existing recording settings; `design_handoff_session_recording` | Existing assets/settings do not prove automatic LiveKit Egress is deployed. Preserve flows and test actual recording policy before native share |

## Concrete risks and prerequisites

1. `remote_control_views.py` restricts requests to teacher context. Remove that restriction only in the reviewed v1 collaboration service, using membership/context checks. No inherited teacher/admin consent.
2. `GroupSessionParticipant.is_sharing_screen` has no producer. Join-time `left_at=None` survives individual departures. Neither field can authorize control. Add short-lived, verified participant bindings and track readiness.
3. Request code calls `int(target_id)` even though users are UUIDs; the consumer compares a UUID to the resulting integer. UUID objects in granted payloads also need explicit string serialization. Legacy code is not safe to activate as-is.
4. `respond_remote_control` directly changes requested→active with no device, lease, fresh membership check, atomic device exclusion or native consent. It must never mint v1 authorization. Old active rows must be expired on migration.
5. Existing grant uniqueness is an `.exists()` check by room, not a database constraint. Reserve the controlled **device**, under a row lock and partial unique constraint, across rooms.
6. CookieJWTAuthentication does not explicitly invoke CSRF validation; DRF API views cannot be assumed protected merely because Django CSRF middleware is installed. New cookie-auth mutations require explicit CSRF enforcement plus exact Origin allowlist. Existing WS auth accepts a legacy query token; do not copy that pattern for new credentials.
7. Secure media ACL reuses stale group presence and staff bypass; it does not establish recipient-only privacy. New private transfer ACLs must have no implicit room-host/staff download grant. Do not regress current room files while introducing private files.
8. `requirements.txt` pins Django 6.0.1, but Dockerfile is Python 3.11; Django 6 requires Python ≥3.12. Compose says PostgreSQL 15, not 16, and launches **Uvicorn ASGI**, not Gunicorn. These are source configuration facts, not proof of deployed versions. Correct/test deployment independently before release.
9. `node shared/sync.mjs --check` failed on six pre-existing copies: AuthContext.jsx, ProfileSwitcher.jsx and SettingsModal.jsx in both dashboards. Do not run blanket sync until ownership/drift is reviewed; new tasks must preserve unrelated edits.
10. LiveKit current online docs describe newer client capabilities than ^2.17.2. The installed student client is exactly 2.17.2; its source contains sendFile, streamBytes and registerByteStreamHandler. Pin actual resolved versions and test cancellation, origin identity and cross-SDK behavior before coding against newer docs.
11. General instant chat membership and join permission do not prove a currently connected LiveKit participant. Bind auth context to a verified LiveKit participant SID and fail closed when that binding becomes stale.

## Unverified

Production Python/Postgres versions, applied migrations, feature flags, LiveKit plan/limits, Egress/recording config, concurrent usage, installed desktop versions, signing credentials, actual installer size/performance, supported browsers and separate Expo repository. No claim of passing backend tests is made in this stage.
