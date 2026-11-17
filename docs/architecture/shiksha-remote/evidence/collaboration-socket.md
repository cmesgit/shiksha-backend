# RC-006b evidence — the ws-ticket route and the collaboration event socket

Date: 17 November 2026. Branch: `docs/shiksha-remote-plan-on-dev`,
cherry-picked to `-on-main`.

**This closes RC-006.** 6a landed the HTTP control plane and the CSRF/Origin
boundary; 6b is the half that could not be validated on dev, because dev runs
gunicorn on :8001 and therefore has no WebSockets at all. Everything below was
exercised locally against `InMemoryChannelLayer` + `LocMemCache`.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/events.py` | the notification envelope, its five event types, and addressed fan-out (**new**) |
| `sessions_app/collaboration/consumers.py` | `CollaborationEventConsumer` — ticket auth, Origin, frame limits, ping (**new**) |
| `sessions_app/collaboration/views.py` | `POST ws-ticket/` added alongside the five RC-006a routes |
| `sessions_app/urls.py` | mounts `collaboration/v1/ws-ticket/` |
| `sessions_app/routing.py` | mounts `ws/collaboration/v1/` |
| `sessions_app/tests_collaboration_ws.py` | **59 tests** (**new**) |

**`config/asgi.py` was NOT modified**, though the task's "Files to modify"
names it. It already includes `sessions_app.routing.websocket_urlpatterns`
wholesale, so adding the route there wires it with a strictly smaller diff
and no risk to the shared middleware stack. Recorded rather than silently
skipped.

No migration. No model change. No existing route, consumer, URL or join flow
was touched.

    POST  /api/sessions/collaboration/v1/ws-ticket/   200 {ticket, expires_in: 30, ws_url}
    WSS   /ws/collaboration/v1/                       ticket-authenticated invalidation bus

## Results

    sessions_app.tests_collaboration_ws                     59 OK   (3 consecutive runs)
    sessions_app.tests_collaboration_policy (RC-006a)       52 OK
    sessions_app.tests_room_file_hardening  (RC-031)        29 OK
    sessions_app.tests_collaboration_baseline + _models     35 OK
    sessions_app livestream global_settings                447 tests, 4 failures + 1 error
    contracts/test_contracts.py + test_openapi.py           13 + 14 OK
    makemigrations --check --dry-run                        No changes detected

🔴 **The 5 non-passes are the permanent environmental baseline** (no LiveKit
credentials locally): `test_request_creates_participant`,
`test_request_with_group_students`, `test_student_can_request_session`
(`RequestSessionTest`), `test_participant_can_view` (`SessionDetailTest`),
`test_start_session` (`SessionLifecycleTest`) — all in `sessions_app.tests`.
The total went **388 → 447** across this task and the failure count did not
move, which is the actual assertion. Listing them by name is deliberate: a
bare "4F+1E" cannot distinguish a preserved baseline from a coincidence.

## What the socket is, and what it refuses to be

The contract's words are *"Pings/invalidation only; REST remains
authoritative"* and *"New WS cannot send arbitrary input or consent
actions."* The inbound vocabulary is therefore exactly two frames —
`authenticate` once, then `ping` — and every other frame closes the socket.

Input does **not** ride this path. Under ADR-02 Option C, pointer and
keyboard travel the LiveKit data channel; Django receives zero input writes,
which is what keeps the feature inside the PostgreSQL connection ceiling.
`SocketVocabularyTest.test_an_input_frame_closes_the_socket` exists to make
that structural rather than conventional: it fires an `input`, a
`pointer.move`, a `grant.decision` and a `subscribe` frame and requires a
close on each. If someone later adds an inbound verb here, that test is the
thing that should stop them long enough to ask the contract owner.

Outbound is the contract's envelope exactly — `{v, event_id, type,
resource_id, version}` — and nothing else. An event is not authority: it
carries an id and a version so the client can refetch the authorized
resource over REST, where the real check lives.

## Four decisions worth reviewing

1. **The Origin rule is split across two points, deliberately.** A *present*
   Origin is checked at the handshake, exactly (never a suffix test). A
   *missing* Origin is not refused there — it is refused at authenticate
   time, if the ticket turns out to be a browser ticket. The contract lets
   the native route omit Origin because a device is identified by its device
   ticket; no device ticket can exist until RC-007, so today every missing
   Origin is refused. Splitting it this way means RC-007 lands the device
   case without reopening the handshake.

2. **`authenticated` and `pong` are additions to the wire contract.** The
   pack defines the server→client *notification* envelope and defines no
   success reply to `authenticate`. Without an ack a client cannot
   distinguish "authenticated" from "silently ignored", so one is sent. It
   is flagged here rather than folded in as though the pack specified it —
   **this needs contract-owner ratification**, and `api-contracts.md` should
   gain a line either way.

3. **A ping re-reads the binding, and that is the socket's expiry path.** The
   binding TTL is 6s; the ticket lives 30s. A socket therefore can outlive
   the binding that justified it. Each ping re-reads the binding from the
   cache and closes the socket if it has lapsed, so a client that stops
   heartbeating loses the socket within one ping interval. **Consequence for
   RC-008: authorization must be re-checked when an event is published,
   never inferred from group membership.** A client that simply never pings
   holds an open socket whose only capability is receiving events addressed
   to a binding — but that is exactly why publish-time checks matter.

4. **Idempotency on `ws-ticket/` has a wart, and the frozen contract requires
   it.** `contracts/openapi.yaml` lists `Idempotency-Key` on this route, so
   replaying a key returns the **same** ticket — which, being single-use with
   a 30s life, may already be spent. That is correct idempotency (the mint
   happened once) rather than a bug, and a client needing a fresh ticket must
   send a fresh key. Pinned by
   `test_replaying_the_same_key_returns_the_same_ticket` and
   `test_a_fresh_key_mints_a_distinct_ticket`.

## Capacity

The box's wall is `max_connections` (~75) with ASGI leaking roughly one
connection per request, so the rules RC-006a made into tests continue here:

* A **ping does no database work at all** — it is one cache read. Only
  `connect()` touches the database, once per socket, to read the feature
  flag.
* The socket **never touches attendance**. `GroupSessionChatConsumer`
  increments `active_connections`, which `group_session_views` uses as the
  room-capacity gate; a control socket that counted would consume a seat and
  push genuine invitees into `409 room_full`, invisibly, because the control
  UI opens *alongside* the chat socket rather than instead of it.
  `SocketAttendanceTest` asserts the counter and the participant-row count
  are unchanged before, during and after.
* Frame limits are the contract's: **8 KiB** and **20 frames/second**. The
  size ceiling is measured in **bytes**, not characters — a multi-byte
  payload passes a naive `len()` at roughly a quarter of its real size, and
  `test_the_size_ceiling_counts_bytes_not_characters` pins it with a 2100-
  character, 8400-byte frame.

## Mutation sweep

24 mutations, one at a time, each run against the single test that should
notice. Harness: `scratchpad/mutate_rc006b.py` (worth recreating per task
rather than keeping).

**Final: 22 caught, 1 equivalent mutant, 1 belt-and-braces check with no test
expected. Every source file verified byte-identical afterwards by hash.**

Caught (21 on the first pass, plus the bytes test after it was fixed):

| Mutation | Caught by |
|---|---|
| origin check removed entirely | `…suffix_origin_is_refused_at_the_handshake` |
| exact origin match weakened to `endswith` | `…suffix_origin_is_refused_at_the_handshake` |
| missing Origin allowed to bypass browser auth | `…missing_origin_cannot_bypass_browser_authentication` |
| **unknown inbound frames tolerated instead of closing** | **`test_an_input_frame_closes_the_socket`** |
| frame size ceiling removed | `test_an_oversized_frame_is_refused` |
| size ceiling counts characters not bytes | `test_the_size_ceiling_counts_bytes_not_characters` ✱ |
| rate limit removed | `test_a_frame_flood_is_rate_limited` |
| binary frames accepted | `test_a_binary_frame_is_refused` |
| cookie/ticket account mismatch tolerated | `…replayed_from_another_accounts_browser_is_refused` |
| pre-auth frames readable | `test_nothing_is_readable_before_authenticating` |
| authenticate frame accepts extra fields | `…authenticate_frame_with_extra_fields_is_refused` |
| 5s auth deadline never fires | `test_an_unauthenticated_socket_expires` |
| ping no longer re-reads the binding | `…ping_drops_the_socket_once_the_binding_lapses` |
| feature flag ignored on the socket | `test_the_socket_is_closed_while_the_flag_is_off` |
| unknown event types delivered instead of raising | `test_an_unknown_event_type_raises` |
| boolean version accepted | `test_a_boolean_version_raises` |
| unaddressed event falls back to a broadcast | `…unaddressed_event_raises_rather_than_broadcasting` |
| principal group drops the context | `…contextless_principal_does_not_collide_with_a_real_context` |
| group names no longer sanitised | `test_group_names_survive_characters_channels_rejects` |
| both ids accepted on `ws-ticket` | `test_sending_both_ids_is_refused` |
| device ticket treated as a browser ticket | `test_a_device_ticket_is_not_yet_available` |
| `ws-ticket` stops checking binding ownership | `test_a_stranger_cannot_mint_for_someone_elses_binding` |

✱ 🔴 **This one SURVIVED the first sweep — a third vacuous negative test in
this project.** `test_the_size_ceiling_counts_bytes_not_characters` built its
frame with a plain `json.dumps`, and the default `ensure_ascii=True` escapes
every `\U0001f534` into a twelve-character ASCII surrogate pair escape. The frame was
therefore **25,235 characters**, over the 8 KiB ceiling by either measure, so
a consumer counting characters rejected it too and the test passed against
the exact bug it was written to catch. Rebuilt with `ensure_ascii=False`
(2,135 characters / 8,435 bytes) and re-verified: it now goes red under the
mutation. The lesson generalises past this repo — **when a test's subject is
an encoding boundary, the fixture must cross that boundary on the wire, not
just in the variable you built it from.**

**Equivalent mutant (1), confirmed by observation rather than argument:**
replacing the "binding lapsed" guard with `record = {}` leaves the socket
closing **4401 anyway**, because the very next check — ticket/binding
identity agreement — compares `record.get("user_id")` (now `None`) against
the ticket's real user id and refuses. Observable behaviour is identical, so
the test is correct and the guard is redundant defence in depth. Both are
kept: the explicit guard gives the accurate reason, and the identity check
protects the outcome if someone later refactors it away.

**No test expected (1):** dropping the ticket/binding identity agreement
itself. Tickets are single-use and minted from the binding they name, so no
reachable path makes those disagree today. Recorded as a deliberate
belt-and-braces check rather than pretending a test covers it.

## 🔴 A process failure worth more than the code

The first run of the mutation harness was killed by a 10-minute shell
timeout **mid-mutation**, and left a live mutation on disk — the
cookie/ticket account-mismatch check replaced by `pass`. Worse, the
integrity check run immediately afterwards **missed it**: it grepped for a
handful of specific guard strings, none of which that mutation touched, and
reported the tree clean.

It surfaced only because a test failed, and was then misread twice — first
as harness teardown noise, then as a flaky test — before the file was
actually read. Two lessons, both now encoded:

1. **A mutation harness must be crash-safe, not merely tidy.** The harness
   now writes originals to disk before touching anything, restores from
   `atexit` *and* from SIGTERM/SIGINT/SIGHUP, and **verifies every file
   byte-for-byte by hash at the end**, printing a loud failure if any
   differs. A `try/finally` is not enough when the process is killed.
2. **"I grepped for the guards" is not an integrity check.** Comparing
   against a known-good snapshot is. The ad-hoc grep was reassuring and
   wrong, which is the worst combination.

Silver lining, and it is a real one: the accidental mutation proved
`test_a_ticket_replayed_from_another_accounts_browser_is_refused` goes red
when its check is removed — before the sweep formally re-proved it.

A second, smaller trap from the same debugging: `asyncio.CancelledError`
derives from **BaseException**, so the first version of the teardown helper
(`except Exception`) caught nothing and changed nothing. That masked the real
assertion failure underneath for two more runs.

## Open items this task did not close

1. ⚠️ **Per-route rate limits are still not implemented** — carried from
   RC-006a. The contract specifies 30/min/user on `csrf/`, 10/min/user on
   `bindings/`, **10/min/principal on `ws-ticket/`**, 30/min/binding on
   `snapshot/`. DRF throttling is already configured in this project, so the
   scopes are additive. The socket's own 20/s frame limit **is** implemented;
   this gap is the HTTP plane only. **Release-gate item.**
2. 🔴 **Guest bindings remain a contract gap** — unchanged by this task and
   still blocking RC-011's OD-01 peer-to-peer story. OD-01 puts
   un-authenticated `InstantCollaborationGuest`s in scope, but every
   `api-contracts.md` row marks `bindings/` as cookie-auth `B`, and no guest
   principal resolver is specified anywhere in the pack. A guest cannot
   obtain a binding, so a guest cannot obtain a ws-ticket, so a guest cannot
   open this socket. **Needs a contract-owner decision, not an
   implementation guess.**
3. ⚠️ **`authenticated`/`pong` need ratifying** — see decision 2 above.
4. 🔴 **None of this is exercisable on dev.** Dev runs gunicorn; a uvicorn
   staging is a hard prerequisite for validating the socket outside the test
   suite, and it is a prerequisite for the RC-016 pilot regardless.

## Next

**RC-007** — paired-device credentials (DPoP pairing, device refresh with
reuse detection, scoped device WS tickets). It is the task that makes the
device branch of this consumer reachable, and it carries a **mandatory
crypto/auth review**. The consumer's device path is already shaped for it:
a ticket with no `binding_id` is refused today at exactly the point where
RC-007 will resolve a `host_binding_id` instead.
