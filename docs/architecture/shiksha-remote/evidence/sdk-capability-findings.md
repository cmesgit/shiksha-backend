# RC-003 partial — SDK capability findings without Windows hardware

Date: 11 November 2026. **Documentation and upstream-source review only.** No Rust was built, no LiveKit room was joined, no Windows machine was used. This does not complete RC-003.

## Why this exists

RC-003 carries five findings, three of them **Stop** criteria that invalidate the architecture rather than inform it. Two of those three are questions about what the LiveKit Rust SDK and server *permit*, not about what a particular PC measures. They are answerable from published sources, and answering them early is the difference between revising an ADR now and revising it after someone has bought a Windows machine and written a capture pipeline.

The third Stop criterion (native screen publish renders in an existing room) and both measurement findings (coordinate fidelity, latency) still require the real spike.

## Finding 1 — **STOP CONFIRMED.** The companion token in ADR-02 cannot receive input.

ADR-02 specifies the companion's grant as `can_publish_data: true` with **`can_subscribe: false` for media**, so the companion can be driven but can never subscribe to anyone's camera or microphone. That distinction does not exist.

`canSubscribe` is documented simply as "Allow participant to subscribe to tracks", and **there is no separate grant for receiving data** — `canPublishData` governs *sending* only. LiveKit [issue #5010](https://github.com/livekit/livekit/issues/5010) settles the direction of travel: it reports that revoking `canSubscribe` failed to stop data packets, treats that as the bug, and the accepted fix (#5011) removes data-track subscriptions on revocation so that data gets "the same lifecycle as media tracks".

So `can_subscribe: false` means the companion receives **no input packets at all**. It would be deaf. The grant ADR-02 specifies does not describe a working participant.

**Why this matters beyond a token tweak.** The point of denying subscription was not tidiness — it was that a companion process running on a student's machine must not be able to receive anyone's audio or video. Setting `can_subscribe: true` to make input work hands it exactly that capability. The defence has to move somewhere else.

ADR-02 anticipated this ("if SDK requires media subscribe permission, do not widen it silently — review the alternative with track permissions") and that instruction is now live. **The alternative is not yet confirmed to exist**: the participant-management documentation describes `UpdateParticipant` for flipping `canSubscribe` but lists no track-level subscription allow-list API. Treat "can the server deny the companion every media track while allowing data" as an **open question**, not a solved one. If the answer is no, the options narrow to accepting a subscribing companion with compensating controls (visible indicator, audited, no local audio sink) or moving control traffic off LiveKit data channels entirely — both ADR-level decisions.

## Finding 2 — **Partially blocking.** Sender identity is not reliably exposed by the Rust SDK today.

The security model's "Controller is actual participant" invariant requires the native core to compare the SDK-reported sender against the grant principals on every packet. In the Rust SDK, `RoomEvent::DataReceived` carries `participant: Option<RemoteParticipant>`, and it is **`None`** whenever the participant lookup misses — which includes the ordinary race where the data channel is open before the server has announced the sender.

The server does stamp the identity on the packet; the SDK drops it. [rust-sdks PR #1574](https://github.com/livekit/rust-sdks/pull/1574) adds a `participant_identity` field to preserve it, and as of this check that PR is **open, not merged**, with no release carrying it.

Not fatal, because the correct behaviour is to fail closed: a packet with no attributable sender must be discarded, never executed. But it means the MVP either discards legitimate input during the join race, or waits on upstream, or vendors the patch. Decide deliberately and write the choice down — "we'll check the sender" is not implementable as specified against a released SDK.

## Finding 3 — unchanged, still needs the spike

Native screen publish rendering in an existing room, WGC↔SendInput coordinate fidelity (≤2 physical pixels) and p95 event→inject latency (<150 ms) are measurements. Nothing here substitutes for them.

## Consequence for sequencing

RC-002 must **not** freeze schemas against ADR-02's current token description — it specifies a participant that cannot work. The token shape and the media-isolation mechanism are now open contract questions, which is exactly the dependency RC-002 has on RC-003.

Recommended order: resolve Finding 1's open question (does a track-level subscription allow-list exist?), revise ADR-02 accordingly, then run the hardware spike for Finding 3. The hardware is no longer the first blocker.
