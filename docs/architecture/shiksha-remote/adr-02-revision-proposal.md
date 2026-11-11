# ADR-02 revision proposal — how the companion receives input

**Status: proposal. Not decided.** Written 11 November 2026 after [evidence/sdk-capability-findings.md](evidence/sdk-capability-findings.md) established that ADR-02's companion token cannot work. The choice below is a security-posture decision for the architecture owner; this document exists so that choice is between two concrete designs rather than a sentence.

## What broke

ADR-02 grants the companion `can_publish_data: true` with `can_subscribe: false`, so it can be driven but can never receive anyone's camera or microphone. LiveKit has no such split: `canSubscribe` governs tracks generally, there is no separate grant for receiving data, and upstream treats data surviving a `canSubscribe` revocation as a bug to fix. The companion as specified receives **no input at all**.

## The reframe that makes this tractable

The obvious reading — "we must grant `canSubscribe`, therefore the companion can spy" — overstates the change, and the existing threat model is why. [permission-security-model.md](permission-security-model.md) already states that **a compromised host OS or maliciously modified companion is outside the guarantee**.

So "the companion binary could subscribe to media" was *already* out of scope. We never relied on the token to defend against our own program misbehaving; we relied on it to defend against **something that is not our program** holding that credential.

That is the risk the revision has to address, and it is narrower: not "can the companion subscribe", but "can anything holding the companion's credential subscribe". Keep that distinction in view — conflating the two is what makes this look unsolvable.

## Option A — subscribing token, client declines to subscribe

Grant `canSubscribe: true`. Connect with auto-subscribe **off** so the companion never subscribes to a track, and never instantiate an audio sink or decoder.

- **Cost:** media isolation moves from a server-enforced signed claim to our own client behaviour. Against a modified companion that is no defence — but that was already outside the guarantee, so nothing real is lost there.
- **Real residual risk:** a stolen device credential used by an *attacker's* client, which would subscribe. Bounded by what already exists in the design — device-bound credentials, short join TTL, and a companion that is a **visible** room participant ("Alice's desktop — Remote Companion"), so an unexpected second companion identity is observable rather than silent.
- **Verify in the spike:** that the Rust SDK exposes an auto-subscribe-off connect option and that declining subscription is actually durable. Do not assume it.

## Option B — move input off LiveKit, onto the existing Django WSS

The companion already holds an authenticated WebSocket to Django for leases. Send input over that and let the LiveKit connection carry only the published screen track.

- **Wins, and they are not small:** no subscribe grant needed at all, so media isolation returns to being server-enforced. **It also fixes Finding 2 for free** — Django knows which authenticated device is on the socket, so sender identity stops depending on an unmerged Rust SDK PR.
- **Cost:** it contradicts ADR-02's "no high-frequency input goes through Django", and that line was not arbitrary. [performance-cost-analysis.md](performance-cost-analysis.md) budgets input at roughly 18 KB/s per control before overhead; twenty concurrent controls is sustained traffic through ASGI rather than through the SFU, on infrastructure already documented as connection-constrained. It also adds a hop (browser → Django → companion) where the SFU path is direct, which spends part of the 150 ms p95 budget on routing.
- **Honest read:** cleanest security story, the one with real capacity risk on current infrastructure.

## Option C — Option A, with authority moved into the payload

Take Option A's transport, and stop treating SDK-reported sender identity as a primary control. Input packets carry the signed epoch and sequence floors the design already specifies; the companion authorises on **that**, with SDK identity used as defence-in-depth when present.

This is mostly a clarification of intent rather than new machinery — the security model already lists "Native nonce freshness + sequence floors + monotonic deadlines, signed epoch". It also dissolves Finding 2's blocking character: if authority lives in a signature the companion can verify, a packet the SDK cannot attribute is rejected on its own merits rather than for lack of a name.

## Recommendation

**Option C**, with Option B held as the fallback if the spike shows auto-subscribe cannot be reliably declined.

Reasoning: it accepts the one genuine loss (media isolation becomes client-side) in the one place the threat model already conceded it, keeps the input path on the SFU where the latency and capacity budgets were calculated, and replaces the weakest link — "trust what the SDK says about the sender" — with a check the companion performs cryptographically instead of a field it reads.

Option B is the better security story in isolation. It is not recommended **first** because its cost lands on infrastructure that is already the platform's documented bottleneck, and a control channel that degrades under load is its own safety problem.

## What this changes downstream

- **ADR-02** needs rewriting: the token grant, and an explicit statement that media isolation is client-side with the reasoning above.
- **RC-002 must not freeze schemas until this is decided** — Option B changes the transport, not just a token field.
- **RC-003's remaining spike** gains one item: confirm auto-subscribe-off is honoured by the Rust SDK.
- **The threat register** needs a row for "stolen device credential used by a non-companion client to subscribe to room media", which no current row covers.
- **Finding 2** stops being blocking under Option C, and disappears under Option B.

## What this does not change

Local native consent, the lease and freshness deadlines, display-capture ownership, and the physical Stop latch are all untouched. This decision is about how input reaches the companion, not about who is allowed to send it — **OD-01 is still open and still blocks RC-011.**
