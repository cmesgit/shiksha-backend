//! The decision logic of the long-running loops, with no I/O.
//!
//! Each driver here is a small state machine driven by a monotonic clock and by
//! *results* that the caller reports. It never sends anything itself: it says
//! what is due, the caller (transport's runtime) does the HTTP, and reports back.
//! That keeps every scheduling and retry rule type-checkable and testable without
//! a network, and keeps the one rule that matters in one place — **when not to
//! retry**.

use crate::clock::Mono;
use crate::companion::InputOrigin;
use crate::credentials::Credentials;

// ── Pairing ───────────────────────────────────────────────────────────────

/// Server: one poll per 5 s. `poll_interval_s` from the server is a floor too.
pub const MIN_POLL_INTERVAL_MS: u64 = 5_000;
/// "5-minute pairing challenge", measured from when the create request was sent.
pub const PAIRING_TTL_MS: u64 = 5 * 60_000;
const MAX_POLL_BACKOFF_MS: u64 = 30_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AbandonReason {
    Expired,
    OwnerDeclined,
    /// 404/410 on poll: the pairing no longer exists server-side.
    ServerGone,
    Rejected(String),
    /// Finalize may have redeemed the secret but we did not get the tokens.
    /// Redemption is atomic and one-time, so this cannot be retried; pair again
    /// (and unlink the orphan from account settings).
    FinalizeUncertain,
    Cancelled,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PairingState {
    Idle,
    Polling,
    AwaitingOwner,
    Finalizing,
    Paired,
    Abandoned(AbandonReason),
}

enum Phase {
    Idle,
    Polling,
    PollInFlight,
    AwaitingOwner,
    ConfirmedLocally,
    Finalizing,
    Paired,
    Abandoned(AbandonReason),
}

#[derive(Clone, PartialEq, Eq)]
pub struct PollRequest {
    pub pairing_id: String,
    pub secret: String,
}

#[derive(Clone, PartialEq, Eq)]
pub struct FinalizeRequest {
    pub pairing_id: String,
    pub secret: String,
    /// When the request is sent; the access token's life runs from here.
    pub started: Mono,
}

impl std::fmt::Debug for PollRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PollRequest").field("pairing_id", &self.pairing_id).finish_non_exhaustive()
    }
}
impl std::fmt::Debug for FinalizeRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FinalizeRequest").field("pairing_id", &self.pairing_id).finish_non_exhaustive()
    }
}

pub enum PollStep {
    NotDue,
    Poll(PollRequest),
    /// The challenge ran out; the flow is now abandoned.
    Expired,
}

pub enum FinalizeStep {
    NotReady,
    Finalize(FinalizeRequest),
    Expired,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PollResult {
    Waiting,
    /// A signed-in human entered the code. The account name and phrase are shown
    /// to the owner, who confirms they match what the browser displays.
    Confirmed { account: String, phrase: String },
    /// 404 / 410.
    Gone,
    RateLimited,
    /// Network error or 5xx. Polling is idempotent, so retrying is safe.
    Transient,
}

#[derive(Clone, PartialEq, Eq)]
pub enum FinalizeResult {
    Paired {
        device_id: String,
        access_token: String,
        refresh_token: String,
        expires_in_s: u64,
    },
    /// Provably never reached the server: safe to try again.
    NotSent,
    /// Timeout after sending, 5xx, an unreadable 200. The secret may be spent.
    Uncertain,
    /// A definitive refusal (wrong secret, expired, already redeemed).
    Rejected { code: String },
}

impl std::fmt::Debug for FinalizeResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FinalizeResult::Paired { device_id, .. } => {
                write!(f, "Paired {{ device_id: {device_id:?}, .. }}")
            }
            FinalizeResult::NotSent => f.write_str("NotSent"),
            FinalizeResult::Uncertain => f.write_str("Uncertain"),
            FinalizeResult::Rejected { code } => write!(f, "Rejected {{ code: {code:?} }}"),
        }
    }
}

pub enum PairingAction {
    /// Show this code; the owner types it into the signed-in browser.
    ShowCode(String),
    /// Ask the owner, **physically**, whether this account and phrase match.
    AskOwner { account: String, phrase: String },
    /// Save before doing anything else with the new credentials.
    Persist { device_id: String, refresh_token: String },
    Paired(Box<Credentials>),
    Abandoned(AbandonReason),
}

impl std::fmt::Debug for PairingAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PairingAction::ShowCode(_) => f.write_str("ShowCode(<code>)"),
            PairingAction::AskOwner { account, .. } => write!(f, "AskOwner {{ account: {account:?}, .. }}"),
            PairingAction::Persist { device_id, .. } => write!(f, "Persist {{ device_id: {device_id:?}, .. }}"),
            PairingAction::Paired(_) => f.write_str("Paired(<credentials>)"),
            PairingAction::Abandoned(r) => write!(f, "Abandoned({r:?})"),
        }
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum PairingRefusal {
    /// A decision arrived from injected input. Only a physical click counts.
    NotPhysical,
    WrongPhase,
}

pub struct PairingFlow {
    phase: Phase,
    id: String,
    secret: String,
    interval_ms: u64,
    next_poll: Mono,
    expires: Mono,
    backoff_ms: u64,
    finalize_started: Mono,
}

impl Default for PairingFlow {
    fn default() -> Self {
        Self::new()
    }
}

impl PairingFlow {
    pub fn new() -> Self {
        Self {
            phase: Phase::Idle,
            id: String::new(),
            secret: String::new(),
            interval_ms: MIN_POLL_INTERVAL_MS,
            next_poll: Mono(0),
            expires: Mono(0),
            backoff_ms: 0,
            finalize_started: Mono(0),
        }
    }

    pub fn state(&self) -> PairingState {
        match &self.phase {
            Phase::Idle => PairingState::Idle,
            Phase::Polling | Phase::PollInFlight => PairingState::Polling,
            Phase::AwaitingOwner => PairingState::AwaitingOwner,
            Phase::ConfirmedLocally | Phase::Finalizing => PairingState::Finalizing,
            Phase::Paired => PairingState::Paired,
            Phase::Abandoned(r) => PairingState::Abandoned(r.clone()),
        }
    }

    fn abandon(&mut self, why: AbandonReason) -> Vec<PairingAction> {
        self.secret.clear();
        self.phase = Phase::Abandoned(why.clone());
        vec![PairingAction::Abandoned(why)]
    }

    /// The create request succeeded. `started` is when it was **sent**.
    pub fn on_started(
        &mut self,
        started: Mono,
        pairing_id: String,
        secret: String,
        user_code: &str,
        poll_interval_s: u64,
    ) -> Vec<PairingAction> {
        if !matches!(self.phase, Phase::Idle) {
            return Vec::new();
        }
        self.id = pairing_id;
        self.secret = secret;
        self.interval_ms = poll_interval_s.saturating_mul(1000).max(MIN_POLL_INTERVAL_MS);
        self.next_poll = started.plus_ms(self.interval_ms);
        self.expires = started.plus_ms(PAIRING_TTL_MS);
        self.phase = Phase::Polling;
        vec![PairingAction::ShowCode(user_code.to_string())]
    }

    pub fn begin_poll(&mut self, now: Mono) -> PollStep {
        if !matches!(self.phase, Phase::Polling) {
            return PollStep::NotDue;
        }
        if now >= self.expires {
            self.abandon(AbandonReason::Expired);
            return PollStep::Expired;
        }
        if now < self.next_poll {
            return PollStep::NotDue;
        }
        self.phase = Phase::PollInFlight;
        PollStep::Poll(PollRequest { pairing_id: self.id.clone(), secret: self.secret.clone() })
    }

    pub fn on_poll(&mut self, now: Mono, r: PollResult) -> Vec<PairingAction> {
        if !matches!(self.phase, Phase::PollInFlight) {
            return Vec::new();
        }
        let wait = |s: &mut Self, extra: u64| {
            s.next_poll = now.plus_ms(s.interval_ms.saturating_add(extra));
            s.phase = Phase::Polling;
        };
        match r {
            PollResult::Waiting => {
                self.backoff_ms = 0;
                wait(self, 0);
                Vec::new()
            }
            PollResult::Confirmed { account, phrase } => {
                self.phase = Phase::AwaitingOwner;
                vec![PairingAction::AskOwner { account, phrase }]
            }
            PollResult::Gone => self.abandon(AbandonReason::ServerGone),
            PollResult::RateLimited | PollResult::Transient => {
                // Doubling, capped: a flapping network must not hammer a
                // route the server already limits to one poll per 5 s.
                self.backoff_ms = (self.backoff_ms.max(self.interval_ms) * 2).min(MAX_POLL_BACKOFF_MS);
                let extra = self.backoff_ms;
                wait(self, extra);
                Vec::new()
            }
        }
    }

    /// The owner's answer. Accepted **only** from a physical click; an injected
    /// one is refused and changes nothing. Allow moves on to finalizing; deny
    /// abandons the pairing and erases the secret.
    pub fn owner_decision(
        &mut self,
        origin: InputOrigin,
        allow: bool,
    ) -> Result<Vec<PairingAction>, PairingRefusal> {
        if origin != InputOrigin::Physical {
            return Err(PairingRefusal::NotPhysical);
        }
        if !matches!(self.phase, Phase::AwaitingOwner) {
            return Err(PairingRefusal::WrongPhase);
        }
        if allow {
            self.phase = Phase::ConfirmedLocally;
            Ok(Vec::new())
        } else {
            Ok(self.abandon(AbandonReason::OwnerDeclined))
        }
    }

    pub fn begin_finalize(&mut self, now: Mono) -> FinalizeStep {
        if !matches!(self.phase, Phase::ConfirmedLocally) {
            return FinalizeStep::NotReady;
        }
        if now >= self.expires {
            self.abandon(AbandonReason::Expired);
            return FinalizeStep::Expired;
        }
        self.phase = Phase::Finalizing;
        self.finalize_started = now;
        FinalizeStep::Finalize(FinalizeRequest {
            pairing_id: self.id.clone(),
            secret: self.secret.clone(),
            started: now,
        })
    }

    pub fn on_finalize(&mut self, r: FinalizeResult) -> Vec<PairingAction> {
        let started = match self.phase {
            Phase::Finalizing => self.finalize_started,
            _ => return Vec::new(),
        };
        match r {
            FinalizeResult::Paired { device_id, access_token, refresh_token, expires_in_s } => {
                self.secret.clear();
                self.phase = Phase::Paired;
                let creds = Credentials::from_pairing(
                    device_id.clone(),
                    access_token,
                    refresh_token.clone(),
                    expires_in_s,
                    started,
                );
                // Persist first: the caller must not proceed on credentials it
                // has not saved.
                vec![
                    PairingAction::Persist { device_id, refresh_token },
                    PairingAction::Paired(Box::new(creds)),
                ]
            }
            FinalizeResult::NotSent => {
                // Never reached the server and the owner already confirmed:
                // allow another attempt without asking again.
                self.phase = Phase::ConfirmedLocally;
                Vec::new()
            }
            FinalizeResult::Uncertain => self.abandon(AbandonReason::FinalizeUncertain),
            FinalizeResult::Rejected { code } => self.abandon(AbandonReason::Rejected(code)),
        }
    }

    /// Abandon the flow if the 5-minute challenge has run out, in **any** live
    /// phase. `begin_poll` and `begin_finalize` check this too, but neither runs
    /// while the flow is waiting for the owner to answer.
    pub fn check_expiry(&mut self, now: Mono) -> Vec<PairingAction> {
        match self.phase {
            Phase::Idle | Phase::Paired | Phase::Abandoned(_) => Vec::new(),
            _ if now >= self.expires => self.abandon(AbandonReason::Expired),
            _ => Vec::new(),
        }
    }

    pub fn cancel(&mut self) -> Vec<PairingAction> {
        match self.phase {
            Phase::Paired | Phase::Abandoned(_) => Vec::new(),
            _ => self.abandon(AbandonReason::Cancelled),
        }
    }
}

// ── Device heartbeat ──────────────────────────────────────────────────────

/// Server: "1/10s idle".
pub const HEARTBEAT_INTERVAL_MS: u64 = 10_000;
pub const HEARTBEAT_MAX_BACKOFF_MS: u64 = 120_000;
/// While the server says the companion is disabled: keep listening, slowly, so a
/// re-enable is noticed without hammering.
pub const HEARTBEAT_DISABLED_INTERVAL_MS: u64 = 60_000;

pub struct HeartbeatSchedule {
    next: Mono,
    fails: u32,
    disabled: bool,
}

impl HeartbeatSchedule {
    pub fn new(now: Mono) -> Self {
        Self { next: now, fails: 0, disabled: false }
    }
    pub fn due(&self, now: Mono) -> bool {
        now >= self.next
    }
    /// The server's kill switch. A companion that sees this must stop sharing and
    /// stop asking for new work; it is not a suggestion.
    pub fn is_disabled(&self) -> bool {
        self.disabled
    }
    pub fn on_success(&mut self, now: Mono, disabled: bool) {
        self.fails = 0;
        self.disabled = disabled;
        let gap = if disabled { HEARTBEAT_DISABLED_INTERVAL_MS } else { HEARTBEAT_INTERVAL_MS };
        self.next = now.plus_ms(gap);
    }
    /// Any failure, including 429. Backs off 10 s, 20 s, 40 s … capped.
    pub fn on_failure(&mut self, now: Mono) {
        self.fails = self.fails.saturating_add(1);
        let shift = self.fails.min(5);
        let gap = (HEARTBEAT_INTERVAL_MS << shift).min(HEARTBEAT_MAX_BACKOFF_MS);
        self.next = now.plus_ms(gap);
    }
}

// ── Lease loop ────────────────────────────────────────────────────────────

/// "device-authenticated every 1s".
pub const LEASE_INTERVAL_MS: u64 = 1_000;
/// A lease request must not outlive the 1 s reply cap by much; the runtime sets
/// its HTTP timeout from this.
pub const LEASE_HTTP_TIMEOUT_MS: u64 = 1_500;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LeaseCallResult {
    /// A signed lease to verify and feed to the core.
    Lease(String),
    /// The access token is dead (rotation elsewhere, or expired): refresh, then
    /// carry on. A single one of these is expected after every rotation.
    TokenRejected,
    /// The server says this grant cannot be renewed. End authority now.
    AuthorityGone(String),
    /// Anything else. Do nothing: the lease deadline is the safety net, and
    /// retrying inside the same second only invites a 429.
    Transient,
}

/// Map a non-2xx lease response. Closing authority on an ambiguous 409 is the
/// safe direction, so unknown 409s count as gone; unknown everything else does
/// not, because ending a grant over a blip is a worse failure than waiting.
pub fn classify_lease_error(status: u16, code: &str) -> LeaseCallResult {
    match status {
        401 => match code {
            "device_token_stale" | "device_auth_required" | "device_token_invalid" => {
                LeaseCallResult::TokenRejected
            }
            "device_revoked" => LeaseCallResult::AuthorityGone(code.to_string()),
            // Clock skew or a replayed jti: the next proof will be right.
            _ => LeaseCallResult::Transient,
        },
        // Includes the feature flag being switched off, which answers 404.
        404 | 410 => LeaseCallResult::AuthorityGone(if code.is_empty() { "gone".into() } else { code.into() }),
        409 => LeaseCallResult::AuthorityGone(if code.is_empty() { "conflict".into() } else { code.into() }),
        _ => LeaseCallResult::Transient,
    }
}

pub struct LeaseDriver {
    next: Mono,
    in_flight: bool,
}

impl LeaseDriver {
    pub fn new(now: Mono) -> Self {
        Self { next: now, in_flight: false }
    }
    /// One at a time, and not more than once per interval.
    pub fn due(&self, now: Mono) -> bool {
        !self.in_flight && now >= self.next
    }
    /// The next renewal is scheduled from when this one **started**, so a slow
    /// reply does not push renewals further apart.
    pub fn begin(&mut self, now: Mono) {
        self.in_flight = true;
        self.next = now.plus_ms(LEASE_INTERVAL_MS);
    }
    pub fn finish(&mut self) {
        self.in_flight = false;
    }
}

// ── Activation failure plan ───────────────────────────────────────────────

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ActivationFailure {
    /// Provably never delivered.
    NotSent,
    /// Sent; no usable reply. The server may now consider the grant active.
    Ambiguous,
    /// A definitive refusal (consent expired, version conflict, …).
    ServerRefused,
    /// A reply arrived but the ticket or lease failed signature or validation.
    /// The server believes the grant is active; we refuse to inject.
    ReplyInvalid,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ActivationPlan {
    /// Tell the server to end the grant (`protocol_error`). Needed whenever it
    /// might believe the grant is active while we will not act on it; "stop wins"
    /// even with a stale version.
    pub terminate: bool,
    /// The same physical consent may be used for another attempt.
    pub retry_same_consent: bool,
}

pub fn activation_plan(f: ActivationFailure) -> ActivationPlan {
    match f {
        ActivationFailure::NotSent => ActivationPlan { terminate: false, retry_same_consent: true },
        ActivationFailure::ServerRefused => ActivationPlan { terminate: false, retry_same_consent: false },
        ActivationFailure::Ambiguous | ActivationFailure::ReplyInvalid => {
            ActivationPlan { terminate: true, retry_same_consent: false }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn started(f: &mut PairingFlow, at: u64) {
        let a = f.on_started(Mono(at), "pid".into(), "SECRET".into(), "ABCD-EFGH", 5);
        assert!(matches!(&a[0], PairingAction::ShowCode(c) if c == "ABCD-EFGH"));
    }

    fn to_awaiting(f: &mut PairingFlow) {
        started(f, 0);
        assert!(matches!(f.begin_poll(Mono(5_000)), PollStep::Poll(_)));
        let a = f.on_poll(Mono(5_100), PollResult::Confirmed { account: "Asha".into(), phrase: "blue otter".into() });
        assert!(matches!(&a[0], PairingAction::AskOwner { account, phrase } if account == "Asha" && phrase == "blue otter"));
    }

    fn paired_result() -> FinalizeResult {
        FinalizeResult::Paired {
            device_id: "dev".into(),
            access_token: "ACC".into(),
            refresh_token: "REF".into(),
            expires_in_s: 300,
        }
    }

    #[test]
    fn the_happy_path_walks_every_step_in_order() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        assert_eq!(f.state(), PairingState::AwaitingOwner);
        assert!(f.owner_decision(InputOrigin::Physical, true).unwrap().is_empty());
        assert!(matches!(f.begin_finalize(Mono(6_000)), FinalizeStep::Finalize(_)));
        let acts = f.on_finalize(paired_result());
        assert!(matches!(&acts[0], PairingAction::Persist { device_id, refresh_token } if device_id == "dev" && refresh_token == "REF"));
        assert!(matches!(&acts[1], PairingAction::Paired(_)));
        assert_eq!(f.state(), PairingState::Paired);
    }

    #[test]
    fn persist_comes_before_the_credentials_are_handed_over() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(6_000));
        let acts = f.on_finalize(paired_result());
        assert!(matches!(acts[0], PairingAction::Persist { .. }));
    }

    #[test]
    fn polling_respects_the_five_second_floor_even_if_the_server_says_less() {
        let mut f = PairingFlow::new();
        f.on_started(Mono(0), "p".into(), "s".into(), "c", 1); // server says 1 s
        assert!(matches!(f.begin_poll(Mono(4_999)), PollStep::NotDue));
        assert!(matches!(f.begin_poll(Mono(5_000)), PollStep::Poll(_)));
    }

    #[test]
    fn a_slower_server_interval_is_honoured() {
        let mut f = PairingFlow::new();
        f.on_started(Mono(0), "p".into(), "s".into(), "c", 8);
        assert!(matches!(f.begin_poll(Mono(7_999)), PollStep::NotDue));
        assert!(matches!(f.begin_poll(Mono(8_000)), PollStep::Poll(_)));
    }

    #[test]
    fn only_one_poll_is_in_flight_and_the_next_waits_a_full_interval_from_the_reply() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        assert!(matches!(f.begin_poll(Mono(5_000)), PollStep::Poll(_)));
        assert!(matches!(f.begin_poll(Mono(5_001)), PollStep::NotDue), "one in flight");
        f.on_poll(Mono(6_000), PollResult::Waiting);
        assert!(matches!(f.begin_poll(Mono(10_999)), PollStep::NotDue));
        assert!(matches!(f.begin_poll(Mono(11_000)), PollStep::Poll(_)));
    }

    #[test]
    fn transient_polls_back_off_15_25_35_then_stay_at_35_seconds_and_waiting_resets_it() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        let mut now = 5_000u64;
        // interval 5 s + backoff: 10 s, 20 s, then capped at 30 s.
        for gap in [15_000u64, 25_000, 35_000, 35_000] {
            assert!(matches!(f.begin_poll(Mono(now)), PollStep::Poll(_)), "due at {now}");
            f.on_poll(Mono(now), PollResult::Transient);
            assert!(matches!(f.begin_poll(Mono(now + gap - 1)), PollStep::NotDue), "gap {gap}");
            now += gap;
        }
        // A good answer clears the backoff: back to the plain 5 s interval.
        assert!(matches!(f.begin_poll(Mono(now)), PollStep::Poll(_)));
        f.on_poll(Mono(now), PollResult::Waiting);
        assert!(matches!(f.begin_poll(Mono(now + 4_999)), PollStep::NotDue));
        assert!(matches!(f.begin_poll(Mono(now + 5_000)), PollStep::Poll(_)));
    }

    #[test]
    fn rate_limiting_backs_off_like_a_transient_error() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        f.begin_poll(Mono(5_000));
        f.on_poll(Mono(5_000), PollResult::RateLimited);
        assert!(matches!(f.begin_poll(Mono(5_000 + 14_999)), PollStep::NotDue));
        assert!(matches!(f.begin_poll(Mono(5_000 + 15_000)), PollStep::Poll(_)));
    }

    #[test]
    fn a_gone_pairing_is_abandoned_and_the_secret_is_erased() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        f.begin_poll(Mono(5_000));
        let a = f.on_poll(Mono(5_100), PollResult::Gone);
        assert!(matches!(&a[0], PairingAction::Abandoned(AbandonReason::ServerGone)));
        assert!(f.secret.is_empty());
        assert!(matches!(f.begin_poll(Mono(99_000)), PollStep::NotDue));
    }

    #[test]
    fn the_challenge_expires_five_minutes_after_the_create_request() {
        let mut f = PairingFlow::new();
        started(&mut f, 1_000);
        assert!(matches!(f.begin_poll(Mono(1_000 + PAIRING_TTL_MS)), PollStep::Expired));
        assert_eq!(f.state(), PairingState::Abandoned(AbandonReason::Expired));
        assert!(f.secret.is_empty());
    }

    #[test]
    fn injected_input_cannot_answer_the_ownership_question() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        assert_eq!(f.owner_decision(InputOrigin::Injected, true).unwrap_err(), PairingRefusal::NotPhysical);
        assert_eq!(f.state(), PairingState::AwaitingOwner, "and nothing changed");
        assert!(matches!(f.begin_finalize(Mono(6_000)), FinalizeStep::NotReady));
        // The physical path through the same call works.
        assert!(f.owner_decision(InputOrigin::Physical, true).is_ok());
    }

    #[test]
    fn finalize_is_impossible_without_the_owners_confirmation() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        assert!(matches!(f.begin_finalize(Mono(6_000)), FinalizeStep::NotReady));
        let mut idle = PairingFlow::new();
        assert!(matches!(idle.begin_finalize(Mono(0)), FinalizeStep::NotReady));
    }

    #[test]
    fn declining_abandons_and_erases_the_secret() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        let a = f.owner_decision(InputOrigin::Physical, false).unwrap();
        assert!(matches!(&a[0], PairingAction::Abandoned(AbandonReason::OwnerDeclined)));
        assert!(f.secret.is_empty());
        assert!(matches!(f.begin_finalize(Mono(6_000)), FinalizeStep::NotReady));
    }

    #[test]
    fn a_decision_outside_the_asking_phase_is_refused() {
        let mut f = PairingFlow::new();
        assert_eq!(f.owner_decision(InputOrigin::Physical, true).unwrap_err(), PairingRefusal::WrongPhase);
        started(&mut f, 0);
        assert_eq!(f.owner_decision(InputOrigin::Physical, true).unwrap_err(), PairingRefusal::WrongPhase);
    }

    #[test]
    fn finalize_that_never_left_the_machine_retries_without_asking_again() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(6_000));
        assert!(f.on_finalize(FinalizeResult::NotSent).is_empty());
        assert!(matches!(f.begin_finalize(Mono(8_000)), FinalizeStep::Finalize(_)));
    }

    #[test]
    fn finalize_that_may_have_redeemed_the_secret_is_never_retried() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(6_000));
        let a = f.on_finalize(FinalizeResult::Uncertain);
        assert!(matches!(&a[0], PairingAction::Abandoned(AbandonReason::FinalizeUncertain)));
        assert!(matches!(f.begin_finalize(Mono(7_000)), FinalizeStep::NotReady));
        assert!(f.secret.is_empty());
    }

    #[test]
    fn a_definitive_finalize_rejection_abandons_with_the_servers_code() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(6_000));
        let a = f.on_finalize(FinalizeResult::Rejected { code: "pairing_expired".into() });
        assert!(matches!(&a[0], PairingAction::Abandoned(AbandonReason::Rejected(c)) if c == "pairing_expired"));
    }

    #[test]
    fn the_confirmation_does_not_outlive_the_challenge() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        assert!(matches!(f.begin_finalize(Mono(PAIRING_TTL_MS)), FinalizeStep::Expired));
    }

    #[test]
    fn the_new_credentials_run_their_clock_from_the_finalize_request() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(100_000));
        let acts = f.on_finalize(paired_result());
        let PairingAction::Paired(c) = &acts[1] else { panic!("not paired") };
        // 300 s from 100_000, with the 5 s use margin.
        assert!(c.access(Mono(100_000 + 294_999)).is_some());
        assert!(c.access(Mono(100_000 + 295_000)).is_none());
    }

    #[test]
    fn the_challenge_expires_even_while_waiting_for_the_owner_to_answer() {
        let mut f = PairingFlow::new();
        to_awaiting(&mut f);
        assert!(f.check_expiry(Mono(PAIRING_TTL_MS - 1)).is_empty());
        let a = f.check_expiry(Mono(PAIRING_TTL_MS));
        assert!(matches!(&a[0], PairingAction::Abandoned(AbandonReason::Expired)));
        assert!(f.secret.is_empty());
        // A late decision after expiry is refused.
        assert_eq!(f.owner_decision(InputOrigin::Physical, true).unwrap_err(), PairingRefusal::WrongPhase);
    }

    #[test]
    fn expiry_checks_leave_idle_and_finished_flows_alone() {
        let mut f = PairingFlow::new();
        assert!(f.check_expiry(Mono(999_999_999)).is_empty());
        to_awaiting(&mut f);
        f.owner_decision(InputOrigin::Physical, true).unwrap();
        f.begin_finalize(Mono(6_000));
        f.on_finalize(paired_result());
        assert!(f.check_expiry(Mono(999_999_999)).is_empty());
        assert_eq!(f.state(), PairingState::Paired);
    }

    #[test]
    fn cancel_abandons_a_live_flow_and_ignores_a_finished_one() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        assert!(matches!(&f.cancel()[0], PairingAction::Abandoned(AbandonReason::Cancelled)));
        assert!(f.cancel().is_empty());
    }

    #[test]
    fn debug_output_hides_the_secret_and_tokens() {
        let mut f = PairingFlow::new();
        started(&mut f, 0);
        let PollStep::Poll(req) = f.begin_poll(Mono(5_000)) else { panic!() };
        let acts = {
            f.on_poll(Mono(5_100), PollResult::Confirmed { account: "A".into(), phrase: "p".into() });
            f.owner_decision(InputOrigin::Physical, true).unwrap();
            f.begin_finalize(Mono(6_000));
            f.on_finalize(paired_result())
        };
        for s in [format!("{req:?}"), format!("{acts:?}"), format!("{:?}", paired_result())] {
            assert!(!s.contains("SECRET") && !s.contains("ACC") && !s.contains("REF"), "{s}");
        }
    }

    // ── heartbeat ──

    #[test]
    fn heartbeat_is_ten_seconds_apart_and_due_immediately_at_start() {
        let mut h = HeartbeatSchedule::new(Mono(0));
        assert!(h.due(Mono(0)));
        h.on_success(Mono(0), false);
        assert!(!h.due(Mono(9_999)));
        assert!(h.due(Mono(10_000)));
    }

    #[test]
    fn heartbeat_failures_back_off_exponentially_and_cap_then_reset_on_success() {
        let mut h = HeartbeatSchedule::new(Mono(0));
        let mut gaps = vec![];
        let mut now = 0u64;
        for _ in 0..8 {
            h.on_failure(Mono(now));
            let mut t = now;
            while !h.due(Mono(t)) {
                t += 1_000;
            }
            gaps.push(t - now);
            now = t;
        }
        assert_eq!(gaps[0], 20_000);
        assert_eq!(gaps[1], 40_000);
        assert!(gaps.iter().all(|&g| g <= HEARTBEAT_MAX_BACKOFF_MS));
        assert_eq!(*gaps.last().unwrap(), HEARTBEAT_MAX_BACKOFF_MS);
        h.on_success(Mono(now), false);
        assert!(h.due(Mono(now + HEARTBEAT_INTERVAL_MS)));
        assert!(!h.due(Mono(now + HEARTBEAT_INTERVAL_MS - 1)));
    }

    #[test]
    fn the_kill_switch_is_remembered_and_slows_the_heartbeat() {
        let mut h = HeartbeatSchedule::new(Mono(0));
        h.on_success(Mono(0), true);
        assert!(h.is_disabled());
        assert!(!h.due(Mono(HEARTBEAT_DISABLED_INTERVAL_MS - 1)));
        assert!(h.due(Mono(HEARTBEAT_DISABLED_INTERVAL_MS)));
        h.on_success(Mono(HEARTBEAT_DISABLED_INTERVAL_MS), false);
        assert!(!h.is_disabled(), "re-enabled");
    }

    // ── lease loop ──

    #[test]
    fn leases_run_once_a_second_one_at_a_time_scheduled_from_the_start() {
        let mut l = LeaseDriver::new(Mono(0));
        assert!(l.due(Mono(0)));
        l.begin(Mono(0));
        assert!(!l.due(Mono(5_000)), "one in flight, however late");
        l.finish();
        // Next is 1 s after the START at t=0, even though the reply took 700 ms.
        assert!(!l.due(Mono(999)));
        assert!(l.due(Mono(1_000)));
    }

    #[test]
    fn lease_errors_classify_the_way_the_server_means_them() {
        use LeaseCallResult::*;
        for code in ["device_token_stale", "device_auth_required", "device_token_invalid"] {
            assert_eq!(classify_lease_error(401, code), TokenRejected, "{code}");
        }
        assert_eq!(classify_lease_error(401, "device_revoked"), AuthorityGone("device_revoked".into()));
        assert_eq!(classify_lease_error(401, "device_proof_invalid"), Transient, "clock skew heals itself");
        assert_eq!(classify_lease_error(401, ""), Transient);
        for (s, c) in [(404, "not_found"), (410, "expired"), (409, "not_active"), (409, "stale_epoch"),
                       (409, "binding_lapsed"), (409, "capture_lost"), (409, "version_conflict")] {
            assert_eq!(classify_lease_error(s, c), AuthorityGone(c.into()), "{s} {c}");
        }
        assert_eq!(classify_lease_error(409, ""), AuthorityGone("conflict".into()));
        // Blips must not end a grant.
        for s in [400u16, 403, 429, 500, 502, 503, 504] {
            assert_eq!(classify_lease_error(s, "rate_limited"), Transient, "{s}");
        }
    }

    // ── activation ──

    #[test]
    fn activation_failures_terminate_exactly_when_the_server_might_think_it_is_active() {
        use ActivationFailure::*;
        let p = |f| activation_plan(f);
        assert_eq!(p(NotSent), ActivationPlan { terminate: false, retry_same_consent: true });
        assert_eq!(p(ServerRefused), ActivationPlan { terminate: false, retry_same_consent: false });
        assert_eq!(p(Ambiguous), ActivationPlan { terminate: true, retry_same_consent: false });
        assert_eq!(p(ReplyInvalid), ActivationPlan { terminate: true, retry_same_consent: false });
    }
}
