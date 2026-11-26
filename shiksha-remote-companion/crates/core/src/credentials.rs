//! Device credential life: when to refresh, and what never to retry.
//!
//! Server behaviour this is built around (`devices.py::rotate_credentials`),
//! each point learned from reading the server, not from the spec:
//!
//! 1. **Refresh tokens are single-use and reuse revokes the device.** Presenting
//!    the *previous* token is read as theft; there is no grace window.
//! 2. **Every rotation bumps `credential_version`, which invalidates every
//!    outstanding access token.** Rotation is also a revocation of whatever came
//!    before it, so a refresh mid-grant makes the next lease 401 until the new
//!    token is in use.
//! 3. The DPoP proof is verified *before* any credential logic, so a proof
//!    rejection is a definitive "nothing was rotated".
//!
//! Consequences encoded below:
//! * never two refreshes in flight (the loser would be treated as reuse);
//! * an ambiguous outcome (sent, no usable reply) is **not** retried at once —
//!   the first request may still be running server-side. It waits, then retries
//!   exactly once. If rotation did happen the device was unrecoverable anyway;
//!   if it did not, the single retry saves the pairing;
//! * the rotated refresh token is handed to the caller to persist **before** it
//!   is used for anything, and a failed persist is a hard failure;
//! * access-token expiry is measured from when the refresh request was *sent*.

use crate::clock::Mono;

/// Refresh when this little access-token life remains. Tokens last 300 s.
pub const REFRESH_MARGIN_MS: u64 = 60_000;
/// Do not use an access token this close to expiry.
pub const USE_MARGIN_MS: u64 = 5_000;
/// How long to wait after an ambiguous refresh before the single retry. Longer
/// than any request timeout, so the first attempt has certainly finished.
pub const UNCERTAIN_WAIT_MS: u64 = 60_000;
pub const NOT_SENT_BACKOFF_MS: u64 = 2_000;
/// Proof rejections (clock skew) tolerated before giving up.
pub const MAX_PROOF_REJECTIONS: u8 = 2;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NeedsPairing {
    NeverPaired,
    /// The server definitively refused the credential (revoked, expired, spent,
    /// unknown). The string is the server's `code`, which is safe to log.
    Rejected(String),
    /// The rotated refresh token could not be saved. The server has already
    /// moved on, so the old one is spent and the new one is lost.
    StoreFailed,
    /// An ambiguous refresh happened twice.
    Uncertain,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Phase {
    Ready,
    Refreshing { retry: bool },
    AwaitingPersist,
    /// Outcome unknown. `retried` is true once the one allowed retry was spent.
    Uncertain { since: Mono, retried: bool },
    Broken(NeedsPairing),
}

/// What a refresh request came back as, already classified by the HTTP layer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RefreshOutcome {
    Rotated { access_token: String, refresh_token: String, expires_in_s: u64 },
    /// 401 `device_proof_invalid`: nothing was rotated. Usually clock skew.
    ProofRejected,
    /// Any other definitive 4xx that means the credential is dead.
    Rejected { code: String },
    /// 429. Nothing was rotated.
    RateLimited { retry_after_s: u64 },
    /// The request provably never reached the server (connection refused, DNS,
    /// TLS handshake). Safe to retry.
    NotSent,
    /// Anything else: timeout after send, 5xx, truncated body.
    Ambiguous,
}

#[derive(Clone, PartialEq, Eq)]
pub enum Effect {
    /// Save this refresh token to protected storage **now**, before using it,
    /// then call [`Credentials::on_persisted`].
    PersistRefresh(String),
    /// The stored credential is dead; erase it so it is never tried again.
    ForgetRefresh,
}

impl std::fmt::Debug for Effect {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Effect::PersistRefresh(_) => f.write_str("PersistRefresh(<redacted>)"),
            Effect::ForgetRefresh => f.write_str("ForgetRefresh"),
        }
    }
}

/// Everything needed to send one refresh request.
#[derive(Clone, PartialEq, Eq)]
pub struct RefreshAttempt {
    pub device_id: String,
    pub refresh_token: String,
    /// When the request is sent. Pass it back to [`Credentials::on_refresh`].
    pub started: Mono,
}

impl std::fmt::Debug for RefreshAttempt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RefreshAttempt")
            .field("device_id", &self.device_id)
            .field("refresh_token", &"<redacted>")
            .field("started", &self.started)
            .finish()
    }
}

pub struct Credentials {
    device_id: String,
    refresh: String,
    access: Option<(String, Mono)>, // token, expiry (monotonic)
    phase: Phase,
    not_before: Mono,
    proof_rejections: u8,
    staged_refresh: Option<String>,
}

impl std::fmt::Debug for Credentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Credentials")
            .field("device_id", &self.device_id)
            .field("phase", &self.phase)
            .finish_non_exhaustive()
    }
}

impl Credentials {
    /// Freshly paired: both tokens just arrived from `finalize`, sent at
    /// `started`.
    pub fn from_pairing(
        device_id: String,
        access_token: String,
        refresh_token: String,
        expires_in_s: u64,
        started: Mono,
    ) -> Self {
        Self {
            device_id,
            refresh: refresh_token,
            access: Some((access_token, started.plus_ms(expires_in_s * 1000))),
            phase: Phase::Ready,
            not_before: Mono(0),
            proof_rejections: 0,
            staged_refresh: None,
        }
    }

    /// Restored from protected storage at startup: a refresh token and no access
    /// token. `refresh_may_be_in_flight` is true when the store still holds the
    /// marker written before a previous refresh was sent — the outcome of which
    /// is unknown, so it goes through the same one-retry path.
    pub fn restored(
        device_id: String,
        refresh_token: String,
        refresh_may_be_in_flight: bool,
        now: Mono,
    ) -> Self {
        let phase = if refresh_may_be_in_flight {
            // Treat as if the wait already elapsed: the previous process is dead,
            // so its request is certainly not still running.
            Phase::Uncertain { since: Mono(now.0.saturating_sub(UNCERTAIN_WAIT_MS)), retried: false }
        } else {
            Phase::Ready
        };
        Self {
            device_id,
            refresh: refresh_token,
            access: None,
            phase,
            not_before: Mono(0),
            proof_rejections: 0,
            staged_refresh: None,
        }
    }

    pub fn unpaired() -> Self {
        Self {
            device_id: String::new(),
            refresh: String::new(),
            access: None,
            phase: Phase::Broken(NeedsPairing::NeverPaired),
            not_before: Mono(0),
            proof_rejections: 0,
            staged_refresh: None,
        }
    }

    pub fn device_id(&self) -> &str {
        &self.device_id
    }

    pub fn needs_pairing(&self) -> Option<&NeedsPairing> {
        match &self.phase {
            Phase::Broken(n) => Some(n),
            _ => None,
        }
    }

    /// An access token safe to put on a request now, if any. Still returned
    /// while a refresh is in flight (it stays valid until the server commits the
    /// rotation); a request that races the commit gets a 401 and calls
    /// [`Self::on_access_rejected`].
    pub fn access(&self, now: Mono) -> Option<&str> {
        match &self.access {
            Some((t, exp)) if now.plus_ms(USE_MARGIN_MS) < *exp => Some(t.as_str()),
            _ => None,
        }
    }

    pub fn is_refreshing(&self) -> bool {
        matches!(self.phase, Phase::Refreshing { .. } | Phase::AwaitingPersist)
    }

    /// Should a refresh start now?
    pub fn needs_refresh(&self, now: Mono) -> bool {
        if now < self.not_before {
            return false;
        }
        match &self.phase {
            Phase::Ready => match &self.access {
                None => true,
                Some((_, exp)) => now.plus_ms(REFRESH_MARGIN_MS) >= *exp,
            },
            Phase::Uncertain { since, retried: false } => now.since(*since) >= UNCERTAIN_WAIT_MS,
            _ => false,
        }
    }

    /// Start a refresh. `None` when none is due, one is already in flight, or
    /// the credential is dead — so a caller can never have two in flight.
    pub fn begin_refresh(&mut self, now: Mono) -> Option<RefreshAttempt> {
        if !self.needs_refresh(now) {
            return None;
        }
        let retry = matches!(self.phase, Phase::Uncertain { .. });
        self.phase = Phase::Refreshing { retry };
        Some(RefreshAttempt {
            device_id: self.device_id.clone(),
            refresh_token: self.refresh.clone(),
            started: now,
        })
    }

    /// The server rejected the access token (401 `device_token_stale`,
    /// `device_auth_required`): stop using it and refresh.
    pub fn on_access_rejected(&mut self) {
        self.access = None;
    }

    pub fn on_refresh(
        &mut self,
        attempt: &RefreshAttempt,
        outcome: RefreshOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        let retry = match self.phase {
            Phase::Refreshing { retry } => retry,
            // A result for a refresh we are not waiting on: ignore it. In
            // particular a late reply must not resurrect a Broken credential.
            _ => return Vec::new(),
        };
        match outcome {
            RefreshOutcome::Rotated { access_token, refresh_token, expires_in_s } => {
                self.access =
                    Some((access_token, attempt.started.plus_ms(expires_in_s.saturating_mul(1000))));
                self.refresh = refresh_token.clone();
                self.staged_refresh = Some(refresh_token.clone());
                self.proof_rejections = 0;
                self.phase = Phase::AwaitingPersist;
                vec![Effect::PersistRefresh(refresh_token)]
            }
            RefreshOutcome::ProofRejected => {
                self.proof_rejections += 1;
                if self.proof_rejections > MAX_PROOF_REJECTIONS {
                    return self.broken(NeedsPairing::Rejected("device_proof_invalid".into()));
                }
                // Nothing was rotated, so the same token is still good; the
                // caller should have corrected its clock offset.
                self.phase = if retry {
                    Phase::Uncertain { since: Mono(now.0.saturating_sub(UNCERTAIN_WAIT_MS)), retried: true }
                } else {
                    Phase::Ready
                };
                Vec::new()
            }
            RefreshOutcome::Rejected { code } => self.broken(NeedsPairing::Rejected(code)),
            RefreshOutcome::RateLimited { retry_after_s } => {
                self.not_before = now.plus_ms(retry_after_s.saturating_mul(1000));
                self.phase = if retry {
                    Phase::Uncertain { since: Mono(now.0.saturating_sub(UNCERTAIN_WAIT_MS)), retried: true }
                } else {
                    Phase::Ready
                };
                Vec::new()
            }
            RefreshOutcome::NotSent => {
                self.not_before = now.plus_ms(NOT_SENT_BACKOFF_MS);
                self.phase = if retry {
                    // Never reached the server, so the earlier ambiguous attempt
                    // is still the only one that might have rotated.
                    Phase::Uncertain { since: Mono(now.0.saturating_sub(UNCERTAIN_WAIT_MS)), retried: false }
                } else {
                    Phase::Ready
                };
                Vec::new()
            }
            RefreshOutcome::Ambiguous => {
                if retry {
                    self.broken(NeedsPairing::Uncertain)
                } else {
                    self.phase = Phase::Uncertain { since: now, retried: false };
                    Vec::new()
                }
            }
        }
    }

    /// The caller saved (or failed to save) the rotated refresh token.
    pub fn on_persisted(&mut self, ok: bool) -> Vec<Effect> {
        if self.phase != Phase::AwaitingPersist {
            return Vec::new();
        }
        self.staged_refresh = None;
        if ok {
            self.phase = Phase::Ready;
            Vec::new()
        } else {
            // The server's copy has moved on and ours is lost. The access token
            // in memory stays usable until it expires so an active session can
            // stop cleanly, but nothing can refresh it.
            self.refresh.clear();
            self.phase = Phase::Broken(NeedsPairing::StoreFailed);
            Vec::new()
        }
    }

    fn broken(&mut self, why: NeedsPairing) -> Vec<Effect> {
        self.refresh.clear();
        self.staged_refresh = None;
        self.phase = Phase::Broken(why);
        vec![Effect::ForgetRefresh]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn paired(now: u64) -> Credentials {
        Credentials::from_pairing("dev".into(), "acc0".into(), "ref0".into(), 300, Mono(now))
    }
    fn rotated(n: u32) -> RefreshOutcome {
        RefreshOutcome::Rotated {
            access_token: format!("acc{n}"),
            refresh_token: format!("ref{n}"),
            expires_in_s: 300,
        }
    }

    #[test]
    fn a_fresh_pairing_is_usable_and_not_yet_due() {
        let c = paired(0);
        assert_eq!(c.access(Mono(1_000)), Some("acc0"));
        assert!(!c.needs_refresh(Mono(1_000)));
    }

    #[test]
    fn refresh_comes_due_a_minute_before_expiry_and_use_stops_five_seconds_before() {
        let c = paired(0); // expires at 300_000
        assert!(!c.needs_refresh(Mono(239_999)));
        assert!(c.needs_refresh(Mono(240_000)));
        assert_eq!(c.access(Mono(294_999)), Some("acc0"));
        assert_eq!(c.access(Mono(295_000)), None);
    }

    #[test]
    fn only_one_refresh_can_be_in_flight() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000));
        assert!(a.is_some());
        assert!(c.is_refreshing());
        assert!(c.begin_refresh(Mono(250_001)).is_none(), "a second would be treated as reuse");
    }

    #[test]
    fn rotation_hands_back_the_new_token_to_persist_before_anything_else() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        assert_eq!(a.refresh_token, "ref0");
        let fx = c.on_refresh(&a, rotated(1), Mono(250_100));
        assert_eq!(fx, vec![Effect::PersistRefresh("ref1".into())]);
        // Not Ready yet: no new refresh may start until the save is confirmed.
        assert!(c.is_refreshing());
        assert!(c.begin_refresh(Mono(1_000_000)).is_none());
        c.on_persisted(true);
        assert!(!c.is_refreshing());
        // New access token expiry runs from when the request was SENT.
        assert_eq!(c.access(Mono(250_000 + 294_999)), Some("acc1"));
        assert_eq!(c.access(Mono(250_000 + 295_000)), None);
    }

    #[test]
    fn the_next_refresh_uses_the_rotated_token_not_the_spent_one() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, rotated(1), Mono(250_100));
        c.on_persisted(true);
        let b = c.begin_refresh(Mono(250_000 + 240_000)).unwrap();
        assert_eq!(b.refresh_token, "ref1");
    }

    #[test]
    fn a_failed_save_is_a_hard_failure_but_the_session_can_finish() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, rotated(1), Mono(250_100));
        c.on_persisted(false);
        assert_eq!(c.needs_pairing(), Some(&NeedsPairing::StoreFailed));
        assert_eq!(c.access(Mono(260_000)), Some("acc1"), "stop cleanly, don't cut off mid-grant");
        assert!(!c.needs_refresh(Mono(10_000_000)));
        assert!(c.begin_refresh(Mono(10_000_000)).is_none());
    }

    #[test]
    fn an_ambiguous_refresh_waits_a_minute_then_retries_exactly_once() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::Ambiguous, Mono(280_000));
        assert!(!c.needs_refresh(Mono(280_001)), "the first request may still be running");
        assert!(!c.needs_refresh(Mono(280_000 + UNCERTAIN_WAIT_MS - 1)));
        assert!(c.needs_refresh(Mono(280_000 + UNCERTAIN_WAIT_MS)));

        let b = c.begin_refresh(Mono(340_000)).unwrap();
        assert_eq!(b.refresh_token, "ref0", "same token: if rotation happened this is the end anyway");
        // Ambiguous again: stop. No third attempt, no hammering.
        let fx = c.on_refresh(&b, RefreshOutcome::Ambiguous, Mono(345_000));
        assert_eq!(fx, vec![Effect::ForgetRefresh]);
        assert_eq!(c.needs_pairing(), Some(&NeedsPairing::Uncertain));
        assert!(c.begin_refresh(Mono(10_000_000)).is_none());
    }

    #[test]
    fn the_single_retry_can_still_succeed() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::Ambiguous, Mono(280_000));
        let b = c.begin_refresh(Mono(340_000)).unwrap();
        let fx = c.on_refresh(&b, rotated(1), Mono(340_100));
        assert_eq!(fx, vec![Effect::PersistRefresh("ref1".into())]);
    }

    #[test]
    fn a_definitive_rejection_kills_the_credential_and_erases_it() {
        for code in ["credential_invalid", "credential_expired", "device_revoked", "device_unknown"] {
            let mut c = paired(0);
            let a = c.begin_refresh(Mono(250_000)).unwrap();
            let fx = c.on_refresh(&a, RefreshOutcome::Rejected { code: code.into() }, Mono(250_100));
            assert_eq!(fx, vec![Effect::ForgetRefresh], "{code}");
            assert_eq!(c.needs_pairing(), Some(&NeedsPairing::Rejected(code.into())));
            assert!(c.begin_refresh(Mono(9_999_999)).is_none());
        }
    }

    #[test]
    fn a_late_reply_cannot_resurrect_a_dead_credential_or_double_apply() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::Rejected { code: "device_revoked".into() }, Mono(250_100));
        assert!(c.on_refresh(&a, rotated(9), Mono(250_200)).is_empty());
        assert!(c.needs_pairing().is_some());
        assert_eq!(c.access(Mono(250_300)), Some("acc0"), "old access unchanged until it expires");
    }

    #[test]
    fn a_proof_rejection_retries_with_the_same_token_then_gives_up() {
        let mut c = paired(0);
        for i in 0..MAX_PROOF_REJECTIONS {
            let a = c.begin_refresh(Mono(250_000 + i as u64)).unwrap();
            assert_eq!(a.refresh_token, "ref0", "nothing was rotated");
            assert!(c.on_refresh(&a, RefreshOutcome::ProofRejected, Mono(250_010)).is_empty());
            assert!(c.needs_pairing().is_none());
        }
        let a = c.begin_refresh(Mono(250_100)).unwrap();
        let fx = c.on_refresh(&a, RefreshOutcome::ProofRejected, Mono(250_110));
        assert_eq!(fx, vec![Effect::ForgetRefresh]);
    }

    #[test]
    fn a_success_resets_the_proof_rejection_count() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::ProofRejected, Mono(250_010));
        let b = c.begin_refresh(Mono(250_020)).unwrap();
        c.on_refresh(&b, rotated(1), Mono(250_030));
        c.on_persisted(true);
        for i in 0..MAX_PROOF_REJECTIONS {
            let x = c.begin_refresh(Mono(500_000 + i as u64)).unwrap();
            c.on_refresh(&x, RefreshOutcome::ProofRejected, Mono(500_010));
        }
        assert!(c.needs_pairing().is_none(), "counter restarted after the success");
    }

    #[test]
    fn rate_limit_and_not_sent_back_off_without_burning_the_token() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::RateLimited { retry_after_s: 30 }, Mono(250_010));
        assert!(!c.needs_refresh(Mono(250_010 + 29_999)));
        assert!(c.needs_refresh(Mono(250_010 + 30_000)));

        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::NotSent, Mono(250_010));
        assert!(!c.needs_refresh(Mono(250_010 + NOT_SENT_BACKOFF_MS - 1)));
        assert!(c.needs_refresh(Mono(250_010 + NOT_SENT_BACKOFF_MS)));
        assert!(c.needs_pairing().is_none());
    }

    #[test]
    fn not_sent_after_an_ambiguous_attempt_keeps_the_single_retry_available() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        c.on_refresh(&a, RefreshOutcome::Ambiguous, Mono(280_000));
        let b = c.begin_refresh(Mono(340_000)).unwrap();
        c.on_refresh(&b, RefreshOutcome::NotSent, Mono(340_010));
        assert!(c.needs_pairing().is_none());
        assert!(c.needs_refresh(Mono(340_010 + NOT_SENT_BACKOFF_MS)), "the retry was not spent");
    }

    #[test]
    fn a_rejected_access_token_triggers_a_refresh() {
        let mut c = paired(0);
        assert!(!c.needs_refresh(Mono(1_000)));
        c.on_access_rejected();
        assert_eq!(c.access(Mono(1_000)), None);
        assert!(c.needs_refresh(Mono(1_000)));
    }

    #[test]
    fn restored_without_an_access_token_refreshes_immediately() {
        let c = Credentials::restored("dev".into(), "ref0".into(), false, Mono(5_000));
        assert_eq!(c.access(Mono(5_000)), None);
        assert!(c.needs_refresh(Mono(5_000)));
    }

    #[test]
    fn restored_after_a_crash_mid_refresh_goes_through_the_one_retry_path() {
        let mut c = Credentials::restored("dev".into(), "ref0".into(), true, Mono(200_000));
        let a = c.begin_refresh(Mono(200_000)).expect("the dead process's request is surely finished");
        let fx = c.on_refresh(&a, RefreshOutcome::Ambiguous, Mono(205_000));
        assert_eq!(fx, vec![Effect::ForgetRefresh], "and only the one retry");
    }

    #[test]
    fn unpaired_has_nothing_to_refresh_with() {
        let mut c = Credentials::unpaired();
        assert_eq!(c.needs_pairing(), Some(&NeedsPairing::NeverPaired));
        assert!(c.begin_refresh(Mono(0)).is_none());
        assert_eq!(c.access(Mono(0)), None);
    }

    #[test]
    fn secrets_never_appear_in_debug_output() {
        let mut c = paired(0);
        let a = c.begin_refresh(Mono(250_000)).unwrap();
        let fx = c.on_refresh(&a, rotated(1), Mono(250_100));
        for s in [format!("{c:?}"), format!("{a:?}"), format!("{fx:?}")] {
            assert!(!s.contains("ref0") && !s.contains("ref1") && !s.contains("acc0"), "{s}");
        }
    }
}
