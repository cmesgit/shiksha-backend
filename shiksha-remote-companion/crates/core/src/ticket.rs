//! Activation-ticket and lease claim validation.
//!
//! Signature verification is *not* here: the transport crate verifies the
//! Ed25519 signature and decodes the claims, then hands this module a
//! [`VerifiedHeader`] and typed claims. Everything the contract says about what
//! the claims must *mean* is enforced here, in plain code with no I/O.
//!
//! Server side: `sessions_app/collaboration/leases.py`.

use crate::wire::is_uuid;

pub const PROTOCOL_VERSION: u64 = 1;
pub const ALGORITHM: &str = "EdDSA";
pub const ACTIVATION_TYP: &str = "shiksha-control+jwt";
pub const LEASE_TYP: &str = "shiksha-control-lease+jwt";
pub const AUDIENCE: &str = "shiksha-remote-companion";
pub const LEASE_TTL_SECONDS: i64 = 3;
/// `nbf` in the future by more than this is refused; the server's clock is not
/// ours and a few seconds of skew is normal.
pub const NBF_SKEW_SECONDS: i64 = 30;

pub const CAPABILITIES: [&str; 5] = [
    "pointer.move",
    "pointer.button",
    "pointer.scroll",
    "keyboard.physical",
    "keyboard.text",
];

/// What the transport saw in the JWT header *and* has already checked the
/// signature against. Constructing one is the transport's claim that the
/// signature verified under `kid`.
#[derive(Clone, Debug)]
pub struct VerifiedHeader {
    pub alg: String,
    pub typ: String,
    pub kid: String,
}

/// Trusted signing keys, pinned. Unknown `kid` fails closed.
pub struct KeySet {
    kids: Vec<String>,
}

impl KeySet {
    pub fn new(kids: Vec<String>) -> Self {
        Self { kids }
    }
    pub fn knows(&self, kid: &str) -> bool {
        self.kids.iter().any(|k| k == kid)
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum TicketError {
    Algorithm,
    Type,
    UnknownKey,
    Audience,
    Protocol,
    NotYetValid,
    Expired,
    Malformed,
    WrongRoom,
    WrongDevice,
    WrongKey,
    WrongBinding,
    WrongCapture,
    WrongCompanion,
    CapabilitySuperset,
    UnknownCapability,
    TooLong,
    ShortLease,
}

impl TicketError {
    pub fn code(self) -> &'static str {
        match self {
            TicketError::Algorithm => "activation_algorithm",
            TicketError::Type => "activation_type",
            TicketError::UnknownKey => "activation_unknown_key",
            TicketError::Audience => "activation_audience",
            TicketError::Protocol => "activation_protocol",
            TicketError::NotYetValid => "activation_not_yet_valid",
            TicketError::Expired => "activation_expired",
            TicketError::Malformed => "activation_malformed",
            TicketError::WrongRoom => "activation_wrong_room",
            TicketError::WrongDevice => "activation_wrong_device",
            TicketError::WrongKey => "activation_wrong_key",
            TicketError::WrongBinding => "activation_wrong_binding",
            TicketError::WrongCapture => "activation_wrong_capture",
            TicketError::WrongCompanion => "activation_wrong_companion",
            TicketError::CapabilitySuperset => "activation_capability_superset",
            TicketError::UnknownCapability => "activation_unknown_capability",
            TicketError::TooLong => "lease_too_long",
            TicketError::ShortLease => "lease_invalid",
        }
    }
}

pub fn check_header(
    h: &VerifiedHeader,
    expected_typ: &str,
    keys: &KeySet,
) -> Result<(), TicketError> {
    // Allowlist of exactly one. `none`, `HS256` (the classic key-confusion
    // downgrade) and anything else are refused before the kid is considered.
    if h.alg != ALGORITHM {
        return Err(TicketError::Algorithm);
    }
    if h.typ != expected_typ {
        return Err(TicketError::Type);
    }
    if !keys.knows(&h.kid) {
        return Err(TicketError::UnknownKey);
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq)]
pub struct ActivationClaims {
    pub aud: String,
    pub protocol: u64,
    pub grant_id: String,
    pub grant_version: u64,
    pub epoch: String,
    pub room_name: String,
    pub controller_identity: String,
    pub controller_sid: String,
    pub companion_identity: String,
    pub companion_sid: String,
    pub device_id: String,
    pub device_thumbprint: String,
    pub host_binding_id: String,
    pub capabilities: Vec<String>,
    pub capture_generation: u64,
    pub track_sid: String,
    pub display_id: String,
    pub nbf: i64,
    pub exp: i64,
}

/// What the local user approved and what this machine already knows. A ticket is
/// accepted only if it describes exactly this, never merely something valid.
#[derive(Clone, Debug)]
pub struct ActivationExpect {
    pub room_name: String,
    pub device_id: String,
    pub device_thumbprint: String,
    pub host_binding_id: String,
    pub capture_generation: u64,
    pub track_sid: String,
    pub display_id: String,
    pub companion_identity: String,
    pub companion_sid: String,
    /// Capabilities shown in the native consent dialog and approved there.
    pub approved_capabilities: Vec<String>,
}

pub fn validate_activation(
    c: &ActivationClaims,
    e: &ActivationExpect,
    wall_now_unix: i64,
) -> Result<(), TicketError> {
    if c.aud != crate::ticket::AUDIENCE {
        return Err(TicketError::Audience);
    }
    if c.protocol != PROTOCOL_VERSION {
        return Err(TicketError::Protocol);
    }
    if !is_uuid(&c.grant_id) || !is_uuid(&c.epoch) {
        return Err(TicketError::Malformed);
    }
    if c.controller_identity.is_empty() || c.controller_sid.is_empty() {
        return Err(TicketError::Malformed);
    }
    if c.nbf > wall_now_unix + NBF_SKEW_SECONDS {
        return Err(TicketError::NotYetValid);
    }
    if c.exp <= wall_now_unix {
        return Err(TicketError::Expired);
    }
    if c.room_name != e.room_name {
        return Err(TicketError::WrongRoom);
    }
    if c.device_id != e.device_id {
        return Err(TicketError::WrongDevice);
    }
    if c.device_thumbprint != e.device_thumbprint {
        return Err(TicketError::WrongKey);
    }
    if c.host_binding_id.is_empty() || c.host_binding_id != e.host_binding_id {
        return Err(TicketError::WrongBinding);
    }
    if c.capture_generation != e.capture_generation
        || c.track_sid != e.track_sid
        || c.display_id != e.display_id
    {
        return Err(TicketError::WrongCapture);
    }
    if c.companion_identity != e.companion_identity || c.companion_sid != e.companion_sid {
        return Err(TicketError::WrongCompanion);
    }
    if c.capabilities.is_empty() {
        return Err(TicketError::Malformed);
    }
    for cap in &c.capabilities {
        if !CAPABILITIES.contains(&cap.as_str()) {
            return Err(TicketError::UnknownCapability);
        }
        // "Reject ... capability supersets": the ticket may carry fewer than the
        // user approved, never more.
        if !e.approved_capabilities.iter().any(|a| a == cap) {
            return Err(TicketError::CapabilitySuperset);
        }
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq)]
pub struct LeaseClaims {
    pub aud: String,
    pub protocol: u64,
    pub grant_id: String,
    pub grant_version: u64,
    pub epoch: String,
    pub device_id: String,
    pub lease_seq: u64,
    pub nonce: String,
    pub activation_hash: String,
    pub iat: i64,
    pub exp: i64,
}

/// A lease's own span, `exp - iat`, in whole seconds, if it is a sane one.
/// The server's wall clock appears only as a *difference*; it is never compared
/// with ours.
pub fn lease_span_seconds(c: &LeaseClaims) -> Result<i64, TicketError> {
    let span = c.exp - c.iat;
    if span <= 0 {
        return Err(TicketError::ShortLease);
    }
    if span > LEASE_TTL_SECONDS {
        return Err(TicketError::TooLong);
    }
    Ok(span)
}

#[cfg(test)]
pub(crate) mod fixtures {
    use super::*;

    pub fn expect() -> ActivationExpect {
        ActivationExpect {
            room_name: "room-1".into(),
            device_id: "dev-1".into(),
            device_thumbprint: "thumb-1".into(),
            host_binding_id: "hb-1".into(),
            capture_generation: 3,
            track_sid: "TR_1".into(),
            display_id: "disp-1".into(),
            companion_identity: "companion-1".into(),
            companion_sid: "PA_comp".into(),
            approved_capabilities: CAPABILITIES.iter().map(|s| s.to_string()).collect(),
        }
    }

    pub fn claims() -> ActivationClaims {
        ActivationClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: "00000000-0000-4000-8000-000000000001".into(),
            grant_version: 4,
            epoch: "00000000-0000-4000-8000-000000000002".into(),
            room_name: "room-1".into(),
            controller_identity: "ctrl-1".into(),
            controller_sid: "PA_ctrl".into(),
            companion_identity: "companion-1".into(),
            companion_sid: "PA_comp".into(),
            device_id: "dev-1".into(),
            device_thumbprint: "thumb-1".into(),
            host_binding_id: "hb-1".into(),
            capabilities: vec!["pointer.move".into(), "pointer.button".into()],
            capture_generation: 3,
            track_sid: "TR_1".into(),
            display_id: "disp-1".into(),
            nbf: 1_000,
            exp: 1_600,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fixtures::*;
    use super::*;

    const NOW: i64 = 1_000;

    #[test]
    fn matching_ticket_is_accepted() {
        assert_eq!(validate_activation(&claims(), &expect(), NOW), Ok(()));
    }

    #[test]
    fn header_allows_only_eddsa_and_pinned_kid() {
        let keys = KeySet::new(vec!["k1".into()]);
        let h = |alg: &str, typ: &str, kid: &str| VerifiedHeader {
            alg: alg.into(),
            typ: typ.into(),
            kid: kid.into(),
        };
        assert_eq!(check_header(&h("EdDSA", ACTIVATION_TYP, "k1"), ACTIVATION_TYP, &keys), Ok(()));
        for bad in ["none", "HS256", "RS256", "ES256", "eddsa"] {
            assert_eq!(
                check_header(&h(bad, ACTIVATION_TYP, "k1"), ACTIVATION_TYP, &keys),
                Err(TicketError::Algorithm),
                "{bad}"
            );
        }
        assert_eq!(
            check_header(&h("EdDSA", LEASE_TYP, "k1"), ACTIVATION_TYP, &keys),
            Err(TicketError::Type),
            "a lease must not pass as a ticket"
        );
        assert_eq!(
            check_header(&h("EdDSA", ACTIVATION_TYP, "rotated-away"), ACTIVATION_TYP, &keys),
            Err(TicketError::UnknownKey)
        );
    }

    #[test]
    fn wrong_audience_protocol_and_times() {
        let mut c = claims();
        c.aud = "someone-else".into();
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::Audience));
        let mut c = claims();
        c.protocol = 2;
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::Protocol));
        let mut c = claims();
        c.nbf = NOW + NBF_SKEW_SECONDS + 1;
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::NotYetValid));
        let mut c = claims();
        c.nbf = NOW + NBF_SKEW_SECONDS;
        assert_eq!(validate_activation(&c, &expect(), NOW), Ok(()));
        let mut c = claims();
        c.exp = NOW;
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::Expired));
    }

    #[test]
    fn right_user_wrong_room_device_sid_or_capture_is_refused() {
        // T06: correct principals, wrong everything else.
        let cases: Vec<(Box<dyn Fn(&mut ActivationClaims)>, TicketError)> = vec![
            (Box::new(|c| c.room_name = "room-2".into()), TicketError::WrongRoom),
            (Box::new(|c| c.device_id = "dev-2".into()), TicketError::WrongDevice),
            (Box::new(|c| c.device_thumbprint = "thumb-2".into()), TicketError::WrongKey),
            (Box::new(|c| c.host_binding_id = "hb-2".into()), TicketError::WrongBinding),
            (Box::new(|c| c.capture_generation = 4), TicketError::WrongCapture),
            (Box::new(|c| c.track_sid = "TR_2".into()), TicketError::WrongCapture),
            (Box::new(|c| c.display_id = "disp-2".into()), TicketError::WrongCapture),
            (Box::new(|c| c.companion_sid = "PA_old".into()), TicketError::WrongCompanion),
        ];
        for (mutate, want) in cases {
            let mut c = claims();
            mutate(&mut c);
            assert_eq!(validate_activation(&c, &expect(), NOW), Err(want));
        }
    }

    #[test]
    fn capability_superset_and_unknown_are_refused() {
        let mut e = expect();
        e.approved_capabilities = vec!["pointer.move".into()];
        // Ticket asks for pointer.button, which the owner did not approve.
        assert_eq!(
            validate_activation(&claims(), &e, NOW),
            Err(TicketError::CapabilitySuperset)
        );
        let mut c = claims();
        c.capabilities = vec!["shell".into()];
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::UnknownCapability));
        let mut c = claims();
        c.capabilities = vec![];
        assert_eq!(validate_activation(&c, &expect(), NOW), Err(TicketError::Malformed));
    }

    #[test]
    fn lease_span_bounds() {
        let mut l = LeaseClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: "g".into(),
            grant_version: 1,
            epoch: "e".into(),
            device_id: "d".into(),
            lease_seq: 1,
            nonce: "n".into(),
            activation_hash: "h".into(),
            iat: 100,
            exp: 103,
        };
        assert_eq!(lease_span_seconds(&l), Ok(3));
        l.exp = 104;
        assert_eq!(lease_span_seconds(&l), Err(TicketError::TooLong));
        l.exp = 100;
        assert_eq!(lease_span_seconds(&l), Err(TicketError::ShortLease));
        l.exp = 90;
        assert_eq!(lease_span_seconds(&l), Err(TicketError::ShortLease));
    }
}
