# RC-009a — host bindings and the companion's identity

Date: 20 November 2026. Branch: `rc008-part2`, on top of RC-008.

A **host binding** ties one paired device to one participant who has
*locally* chosen to share one display in one room. It is the thing RC-008
kept referring to and could not resolve.

## Delivered

| File | What |
|---|---|
| `sessions_app/collaboration/host_bindings.py` | the lifecycle store (**new**) |
| `sessions_app/collaboration/companion_tokens.py` | identity + screen-only token (**new**) |
| `sessions_app/collaboration/host_binding_views.py` | seven routes (**new**) |
| `sessions_app/tests_companion_lifecycle.py` | **34 tests** (**new**) |
| `sessions_app/tests_host_binding_routes.py` | **15 tests** (**new**) |
| `leases.py`, `grant_views.py`, `urls.py` | the two RC-008 seams, closed |

    → 157 tests, OK (RC-008's suites included — both were affected)

## The two seams RC-008 left, now closed

**`POST grants/` returns 201.** It previously could only refuse: the target
and device are derived from the host binding, and there was none. The test
asserts the request body contains no controller or target id — both are
derived server-side, which is the contract's "no client controller/target
IDs" and the reason anyone cannot push a consent prompt at anyone.

**The host half of the 3-second lease rule is enforced.**
`leases._host_bindings_exist` is gone. A renewal now refuses `capture_lost`
when the share is not `ready`, and `binding_stale` when its heartbeat is
older than 3s — the same bound the controller side already had.

## 🔴 The asymmetry that is the whole security story

An `rcdev_` prefix is enough to **exclude** a participant from human
accounting, and nowhere near enough to **trust** one as a companion.

* `looks_like_companion` is deliberately permissive and fail-safe. The worst
  a forged prefix achieves is excluding itself from a count it was never
  entitled to appear in.
* `registered_companion` is the only path to trust: a live host binding, in
  a live state, for this room. RC-009's acceptance criteria say it directly
  — "Prevent unknown prefixed identities being treated as valid companion."

Getting those backwards in either direction is a real vulnerability, so both
are asserted explicitly.

## 🔴 Two bugs found, both by tests that expected success

**A closed share kept vouching for its companion.** Terminal records are
kept for 30 seconds so a polling client learns *why* its share ended rather
than meeting a bare 404. That courtesy leaked into trust:
`registered_companion` returned the tombstone, so a stopped share continued
to authorise its identity for those 30 seconds. That is the "stale token
cannot grant input" criterion failing.

**Closing a share left pending requests alive.** `capture_lost` maps to
`failed`, and the protocol's table only permits `failed` from
`approved/active/paused` — all states that have already been approved. A
grant still at `requested` cannot legally become `failed`, so
`terminate_for_binding` caught the `GrantError` and moved on. The pending
request survived until its own 60-second deadline: a consent prompt on the
target's screen, for a display nobody was sharing.

`grants.terminate` now falls back to `revoked` when a reason's mapped state
is not reachable from the current one, preserving the original reason so the
audit still records what happened. Both directions are tested — a
`requested` grant becomes `revoked`, an `active` one still becomes `failed`.

## Decisions worth knowing

**The companion token grants `can_subscribe: true`, and must.** ADR-02
originally specified `can_subscribe: false`; RC-003 established that LiveKit
has no data/media split, so that token would make the companion **deaf** —
no input at all. Option C moves media isolation client-side (connect with
auto-subscribe off, no audio sink) and authority into the signed payload.
`test_the_token_grants_subscribe_and_that_is_adr02_option_c` exists so
nobody "hardens" it back to False and breaks the feature while believing
they improved it.

**Publishing is pinned to `screen_share`** via `can_publish_sources`, set
defensively: an older `livekit-api` without the field would raise at
construction and take the share route down entirely, which is worse than a
broader token. When it cannot be applied the fact is logged loudly, because
without it nothing but our own client behaviour stops the companion
publishing a camera.

**The identity is bound to the host binding, not the device or user** —
`rcdev_<host_binding_uuid>`. It names one share of one display in one room,
cannot be replayed into another room, and becomes meaningless when that
binding closes.

**Only the device can share, declare ready or heartbeat.** The owner's
browser creates a binding and can stop one. "No browser API can cause
unattended capture" is enforced by that split;
`test_a_browser_cannot_start_a_capture` is the test to keep, because if
`share` ever accepts a cookie this feature has a remote screen-capture hole
and every other test still passes.

**Missing LiveKit credentials are a 503, not a 500.** The SDK raises
`ValueError`; a deployment without credentials is a dependency problem the
companion should back off and retry from, which an opaque server error does
not communicate.

**`allow_requests` defaults False.** Sharing a screen is not offering it for
control. Turning it off cancels pending requests but deliberately does not
terminate an active grant — active control ends by explicit Stop, so a
flipped switch cannot drop someone mid-session without the Stop path's
ordering guarantees running.

## What this does NOT do

* **`snapshot` still returns `host_bindings: []`.** The data now exists and
  `public_availability` is written and tested; wiring it into the snapshot
  response is left to RC-009b with the rest of the integration work.
* **No attendance or presence exclusion yet.** This is RC-009b and it is the
  half that matters most for "no attendance or capacity inflation":
  `group_attendance.parse_user_id` splits on `_` and returns `parts[0]`, so
  an `rcdev_…` identity currently yields **`"rcdev"` as a user id** and
  flows into attendance intervals. The exclusion must happen *before* that
  parse.
* **SID verification is untested against a real LiveKit.** `verify_sid` is
  injected and the tests supply a stub, following RC-006's precedent of
  patching the roster. There are no LiveKit credentials in this environment
  — the same gap behind the pre-existing `sessions_app.tests` failures.

## Next

**RC-009b** — exclude `rcdev_` identities from presence, attendance and
capacity before legacy parsing; wire `host_bindings` into `snapshot`; and
confirm the existing human webhook tests still pass unchanged.
