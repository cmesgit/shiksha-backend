//! Lease tracking: the monotonic deadline that bounds revoked authority to 3 s.
//!
//! Rules from `remote-control-protocol.md` §Lease and local gate:
//! * only a reply to the **currently outstanding** nonce can renew;
//! * replies taking more than 1 s are discarded;
//! * the deadline is `min(request_start + 3 s, server span)` measured from when
//!   the request was **sent**, never from when the reply arrived;
//! * `lease_seq` must strictly increase;
//! * a reply after a local stop / generation change is discarded.

use crate::clock::{b64url, Entropy, Mono};
use crate::ticket::{lease_span_seconds, LeaseClaims, TicketError, LEASE_TTL_SECONDS};

pub const MAX_RESPONSE_AGE_MS: u64 = 1_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LeaseError {
    NoOutstandingRequest,
    NonceMismatch,
    /// Took longer than [`MAX_RESPONSE_AGE_MS`].
    Late,
    /// The stop latch moved since the request was sent.
    StaleGeneration,
    WrongGrant,
    WrongEpoch,
    WrongDevice,
    WrongActivation,
    SequenceNotIncreasing,
    VersionWentBackwards,
    Claims(TicketError),
}

#[derive(Clone, Debug)]
struct Outstanding {
    nonce: String,
    started: Mono,
    generation: u64,
}

/// What the app must put in the `POST /grants/{id}/lease` body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaseRequest {
    pub nonce: String,
    pub epoch: String,
    pub expected_version: u64,
}

pub struct LeaseTracker {
    grant_id: String,
    epoch: String,
    device_id: String,
    activation_hash: String,
    version: u64,
    last_seq: u64,
    outstanding: Option<Outstanding>,
    deadline: Option<Mono>,
}

impl LeaseTracker {
    pub fn new(
        grant_id: String,
        epoch: String,
        device_id: String,
        activation_hash: String,
        grant_version: u64,
    ) -> Self {
        Self {
            grant_id,
            epoch,
            device_id,
            activation_hash,
            version: grant_version,
            last_seq: 0,
            outstanding: None,
            deadline: None,
        }
    }

    /// Start a renewal. Replaces any request still outstanding, whose reply will
    /// then fail the nonce check — so a slow reply can never beat a newer one.
    pub fn begin(&mut self, now: Mono, generation: u64, entropy: &mut dyn Entropy) -> LeaseRequest {
        let nonce = b64url(&entropy.bytes16());
        self.outstanding = Some(Outstanding { nonce: nonce.clone(), started: now, generation });
        LeaseRequest { nonce, epoch: self.epoch.clone(), expected_version: self.version }
    }

    /// Treat a request already sent (`activate`/`resume`, which carry a
    /// `lease_nonce` and return the first lease in their reply) as the
    /// outstanding renewal. `started` is when that request was **sent**, so the
    /// deadline is measured from it exactly as for any renewal.
    pub fn adopt(&mut self, nonce: String, started: Mono, generation: u64) {
        self.outstanding = Some(Outstanding { nonce, started, generation });
    }

    /// Apply a server reply that the transport has already signature-checked.
    /// `now` is the monotonic time the reply was *received*; `latch_generation`
    /// is the stop latch's current generation.
    pub fn accept(
        &mut self,
        claims: &LeaseClaims,
        now: Mono,
        latch_generation: u64,
    ) -> Result<Mono, LeaseError> {
        let out = self.outstanding.clone().ok_or(LeaseError::NoOutstandingRequest)?;
        if out.nonce != claims.nonce {
            // Not consumed: the genuine reply may still be on its way.
            return Err(LeaseError::NonceMismatch);
        }
        // From here the outstanding request is spent whatever happens.
        self.outstanding = None;

        if now.since(out.started) > MAX_RESPONSE_AGE_MS {
            return Err(LeaseError::Late);
        }
        if out.generation != latch_generation {
            return Err(LeaseError::StaleGeneration);
        }
        if claims.grant_id != self.grant_id {
            return Err(LeaseError::WrongGrant);
        }
        if claims.epoch != self.epoch {
            return Err(LeaseError::WrongEpoch);
        }
        if claims.device_id != self.device_id {
            return Err(LeaseError::WrongDevice);
        }
        if claims.activation_hash != self.activation_hash {
            // A lease for the same grant but a different ticket.
            return Err(LeaseError::WrongActivation);
        }
        if claims.lease_seq <= self.last_seq {
            return Err(LeaseError::SequenceNotIncreasing);
        }
        if claims.grant_version < self.version {
            return Err(LeaseError::VersionWentBackwards);
        }
        let span = lease_span_seconds(claims).map_err(LeaseError::Claims)?;

        let window_ms = (span.min(LEASE_TTL_SECONDS) as u64) * 1000;
        // Measured from the request's start: a slow reply shortens the window.
        let deadline = out.started.plus_ms(window_ms);
        self.last_seq = claims.lease_seq;
        self.version = claims.grant_version;
        self.deadline = Some(deadline);
        Ok(deadline)
    }

    /// Deadline of the freshest accepted lease, if any.
    pub fn deadline(&self) -> Option<Mono> {
        self.deadline
    }

    pub fn is_fresh(&self, now: Mono) -> bool {
        matches!(self.deadline, Some(d) if now < d)
    }

    pub fn last_seq(&self) -> u64 {
        self.last_seq
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::CountingEntropy;
    use crate::ticket::AUDIENCE;

    fn tracker() -> LeaseTracker {
        LeaseTracker::new("g".into(), "e".into(), "d".into(), "h".into(), 4)
    }

    fn reply(nonce: &str, seq: u64) -> LeaseClaims {
        LeaseClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: "g".into(),
            grant_version: 4,
            epoch: "e".into(),
            device_id: "d".into(),
            lease_seq: seq,
            nonce: nonce.into(),
            activation_hash: "h".into(),
            iat: 1000,
            exp: 1003,
        }
    }

    #[test]
    fn deadline_is_request_start_plus_three_seconds_not_receipt() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let req = t.begin(Mono(10_000), 0, &mut en);
        // Reply arrives 900 ms later (inside the 1 s cap).
        let d = t.accept(&reply(&req.nonce, 1), Mono(10_900), 0).unwrap();
        assert_eq!(d, Mono(13_000), "request_start + 3000, not receive + 3000");
        assert!(t.is_fresh(Mono(12_999)));
        assert!(!t.is_fresh(Mono(13_000)));
    }

    #[test]
    fn late_reply_is_discarded_and_grants_nothing() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let req = t.begin(Mono(0), 0, &mut en);
        assert_eq!(
            t.accept(&reply(&req.nonce, 1), Mono(1_001), 0),
            Err(LeaseError::Late)
        );
        assert_eq!(t.deadline(), None);
        // And it cannot be retried: the nonce is spent.
        assert_eq!(
            t.accept(&reply(&req.nonce, 1), Mono(1_002), 0),
            Err(LeaseError::NoOutstandingRequest)
        );
    }

    #[test]
    fn reply_exactly_at_one_second_is_accepted() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let req = t.begin(Mono(0), 0, &mut en);
        assert!(t.accept(&reply(&req.nonce, 1), Mono(1_000), 0).is_ok());
    }

    #[test]
    fn only_the_outstanding_nonce_renews() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let first = t.begin(Mono(0), 0, &mut en);
        let second = t.begin(Mono(500), 0, &mut en);
        // The reply to the superseded request must not renew.
        assert_eq!(
            t.accept(&reply(&first.nonce, 1), Mono(600), 0),
            Err(LeaseError::NonceMismatch)
        );
        assert_eq!(t.deadline(), None);
        // The genuine one still can: a mismatch did not burn it.
        assert!(t.accept(&reply(&second.nonce, 1), Mono(700), 0).is_ok());
    }

    #[test]
    fn reply_with_no_request_outstanding_is_refused() {
        let mut t = tracker();
        assert_eq!(
            t.accept(&reply("anything", 1), Mono(0), 0),
            Err(LeaseError::NoOutstandingRequest)
        );
    }

    #[test]
    fn reply_after_a_local_stop_is_discarded() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let req = t.begin(Mono(0), 7, &mut en);
        // Latch generation moved 7 -> 8 while the request was in flight.
        assert_eq!(
            t.accept(&reply(&req.nonce, 1), Mono(100), 8),
            Err(LeaseError::StaleGeneration)
        );
        assert_eq!(t.deadline(), None);
    }

    #[test]
    fn sequence_must_strictly_increase() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let r1 = t.begin(Mono(0), 0, &mut en);
        t.accept(&reply(&r1.nonce, 5), Mono(10), 0).unwrap();
        let r2 = t.begin(Mono(1000), 0, &mut en);
        assert_eq!(
            t.accept(&reply(&r2.nonce, 5), Mono(1010), 0),
            Err(LeaseError::SequenceNotIncreasing)
        );
        let r3 = t.begin(Mono(2000), 0, &mut en);
        assert_eq!(
            t.accept(&reply(&r3.nonce, 4), Mono(2010), 0),
            Err(LeaseError::SequenceNotIncreasing)
        );
        let r4 = t.begin(Mono(3000), 0, &mut en);
        assert!(t.accept(&reply(&r4.nonce, 6), Mono(3010), 0).is_ok());
    }

    #[test]
    fn rejected_reply_leaves_the_previous_deadline_unextended() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let r1 = t.begin(Mono(0), 0, &mut en);
        let d1 = t.accept(&reply(&r1.nonce, 1), Mono(10), 0).unwrap();
        let r2 = t.begin(Mono(1000), 0, &mut en);
        let _ = t.accept(&reply(&r2.nonce, 1), Mono(1010), 0); // duplicate seq
        assert_eq!(t.deadline(), Some(d1));
    }

    #[test]
    fn forged_fields_are_refused() {
        let cases: Vec<(Box<dyn Fn(&mut LeaseClaims)>, LeaseError)> = vec![
            (Box::new(|c| c.grant_id = "other".into()), LeaseError::WrongGrant),
            (Box::new(|c| c.epoch = "old".into()), LeaseError::WrongEpoch),
            (Box::new(|c| c.device_id = "other".into()), LeaseError::WrongDevice),
            (Box::new(|c| c.activation_hash = "x".into()), LeaseError::WrongActivation),
            (Box::new(|c| c.grant_version = 3), LeaseError::VersionWentBackwards),
            (Box::new(|c| c.exp = c.iat + 4), LeaseError::Claims(TicketError::TooLong)),
        ];
        for (mutate, want) in cases {
            let (mut t, mut en) = (tracker(), CountingEntropy::default());
            let req = t.begin(Mono(0), 0, &mut en);
            let mut c = reply(&req.nonce, 1);
            mutate(&mut c);
            assert_eq!(t.accept(&c, Mono(10), 0), Err(want.clone()), "{want:?}");
            assert_eq!(t.deadline(), None);
        }
    }

    #[test]
    fn server_span_shorter_than_three_seconds_shortens_the_window() {
        let (mut t, mut en) = (tracker(), CountingEntropy::default());
        let req = t.begin(Mono(0), 0, &mut en);
        let mut c = reply(&req.nonce, 1);
        c.exp = c.iat + 1;
        assert_eq!(t.accept(&c, Mono(10), 0), Ok(Mono(1_000)));
    }
}
