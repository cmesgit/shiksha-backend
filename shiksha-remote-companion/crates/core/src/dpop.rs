//! RFC 9449 DPoP proof assembly, client side.
//!
//! The server half is `sessions_app/collaboration/dpop.py`; its profile is the
//! contract and this module is its mirror, with no options:
//!
//! `typ=dpop+jwt` · `alg=ES256` · public JWK in the header · `htm` · `htu` ·
//! `iat` · `jti` (≥16 chars) · `ath` on resource requests, and **never** on an
//! unauthenticated route (the server rejects an unexpected `ath`).
//!
//! Hashing and signing are injected. That keeps this crate dependency-free and
//! lets the transport crate supply audited implementations (`sha2`, `p256`) —
//! the architecture is explicit: "Do not implement elliptic-curve cryptography
//! manually". What *is* here is everything a reviewer can get wrong around the
//! crypto: which bytes are signed, the canonical thumbprint input, which claims
//! appear on which route, and what the `htu` may contain.

use crate::clock::{b64url, Entropy};

pub const TYP: &str = "dpop+jwt";
pub const ALG: &str = "ES256";
/// The server refuses a proof over 4096 bytes before looking at it.
pub const MAX_PROOF_BYTES: usize = 4096;

pub trait Hasher {
    fn sha256(&self, data: &[u8]) -> [u8; 32];
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignError(pub &'static str);

/// A P-256 key that signs for ES256.
///
/// `sign_es256` must return the 64-byte JWS form `r || s` (big-endian, 32 bytes
/// each) — **not** DER. A DER signature is the classic ES256 interoperability
/// failure and the server reports it only as `signature_invalid`.
pub trait ProofSigner {
    fn sign_es256(&self, signing_input: &[u8]) -> Result<[u8; 64], SignError>;
    /// Affine coordinates of the public key, big-endian, 32 bytes each.
    fn public_xy(&self) -> ([u8; 32], [u8; 32]);
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DpopError {
    /// `htu` must be an absolute URL with no query and no fragment.
    BadHtu,
    BadMethod,
    Sign(SignError),
    TooLarge,
}

/// The JWK exactly as RFC 7638 canonicalises it for a P-256 key: members
/// `crv, kty, x, y` in that (alphabetical) order, no whitespace. The same string
/// is used as the header's `jwk`, which the server re-canonicalises itself.
pub fn jwk_json(x: &[u8; 32], y: &[u8; 32]) -> String {
    format!(
        r#"{{"crv":"P-256","kty":"EC","x":"{}","y":"{}"}}"#,
        b64url(x),
        b64url(y)
    )
}

/// RFC 7638 thumbprint: base64url(SHA-256(canonical JWK)). Matches
/// `dpop.jwk_thumbprint` on the server and the `cnf.jkt` in access tokens.
pub fn thumbprint(h: &dyn Hasher, x: &[u8; 32], y: &[u8; 32]) -> String {
    b64url(&h.sha256(jwk_json(x, y).as_bytes()))
}

/// `ath`: base64url(SHA-256(access token)).
pub fn access_token_hash(h: &dyn Hasher, access_token: &str) -> String {
    b64url(&h.sha256(access_token.as_bytes()))
}

/// Escape for embedding in a JSON string. Inputs here are URLs, methods and
/// base64url, so this is belt-and-braces, but a proof is signed bytes and a
/// stray quote would produce a signed, malformed token.
fn esc(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o
}

/// Is `htu` acceptable? Absolute, `https` (or `http` to a loopback host for
/// local development only), and no query or fragment — RFC 9449 §4.2.
pub fn check_htu(htu: &str) -> Result<(), DpopError> {
    if htu.contains('?') || htu.contains('#') || htu.chars().any(|c| c.is_whitespace()) {
        return Err(DpopError::BadHtu);
    }
    if let Some(rest) = htu.strip_prefix("https://") {
        return if rest.is_empty() || rest.starts_with('/') { Err(DpopError::BadHtu) } else { Ok(()) };
    }
    if let Some(rest) = htu.strip_prefix("http://") {
        let host = rest.split(['/', ':']).next().unwrap_or("");
        let loopback = host == "localhost" || host == "127.0.0.1" || host == "[::1]";
        return if loopback { Ok(()) } else { Err(DpopError::BadHtu) };
    }
    Err(DpopError::BadHtu)
}

/// The `htu` the server will compute for `path`: the configured canonical base
/// joined with the route's relative path (`dpop.canonical_url`). The base is
/// operator configuration on both sides; nothing a response says may change it.
pub fn htu_for(canonical_base: &str, path: &str) -> Result<String, DpopError> {
    let htu = format!("{}/{}", canonical_base.trim_end_matches('/'), path.trim_start_matches('/'));
    check_htu(&htu)?;
    Ok(htu)
}

pub struct ProofRequest<'a> {
    /// HTTP method, e.g. `POST`. Uppercased here.
    pub method: &'a str,
    pub htu: &'a str,
    /// Seconds since the Unix epoch **as the server will see them**: local wall
    /// time plus the learned server offset. Wall time is used here and only
    /// here; proofs live ±30 s.
    pub iat: i64,
    /// `Some` on a resource request (adds `ath`), `None` on pairing and refresh.
    pub access_token: Option<&'a str>,
}

/// Build one proof. A fresh `jti` is drawn every call: the server burns each for
/// two minutes, so reusing a proof is a replay and is refused.
pub fn build_proof(
    signer: &dyn ProofSigner,
    hasher: &dyn Hasher,
    entropy: &mut dyn Entropy,
    req: &ProofRequest<'_>,
) -> Result<String, DpopError> {
    check_htu(req.htu)?;
    let method = req.method.to_ascii_uppercase();
    if method.is_empty() || !method.bytes().all(|b| b.is_ascii_uppercase()) {
        return Err(DpopError::BadMethod);
    }

    let (x, y) = signer.public_xy();
    let header = format!(
        r#"{{"alg":"{ALG}","typ":"{TYP}","jwk":{}}}"#,
        jwk_json(&x, &y)
    );
    let jti = b64url(&entropy.bytes16()); // 22 chars: server requires >= 16
    let mut payload = format!(
        r#"{{"htm":"{}","htu":"{}","iat":{},"jti":"{}""#,
        esc(&method),
        esc(req.htu),
        req.iat,
        jti
    );
    if let Some(token) = req.access_token {
        payload.push_str(&format!(r#","ath":"{}""#, access_token_hash(hasher, token)));
    }
    payload.push('}');

    let signing_input = format!("{}.{}", b64url(header.as_bytes()), b64url(payload.as_bytes()));
    let sig = signer.sign_es256(signing_input.as_bytes()).map_err(DpopError::Sign)?;
    let proof = format!("{}.{}", signing_input, b64url(&sig));
    if proof.len() > MAX_PROOF_BYTES {
        return Err(DpopError::TooLarge);
    }
    Ok(proof)
}

/// Learned difference between the server's clock and ours, in seconds.
///
/// Proofs are only valid within ±30 s of the server's time, and school laptops
/// have wrong clocks. The offset is learned from the server's own reported time
/// (`server_time` in a heartbeat reply, or the HTTP `Date` header) and applied to
/// `iat`. It never touches a monotonic deadline.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct ClockOffset(i64);

impl ClockOffset {
    pub fn learn(server_unix: i64, local_unix: i64) -> Self {
        Self(server_unix - local_unix)
    }
    pub fn apply(self, local_unix: i64) -> i64 {
        local_unix + self.0
    }
    pub fn seconds(self) -> i64 {
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::CountingEntropy;
    use std::cell::RefCell;

    /// Records what it was asked to hash, and "hashes" by taking the first 32
    /// bytes of a repeating pattern of the input length - distinct per input.
    struct FakeHasher {
        seen: RefCell<Vec<Vec<u8>>>,
    }
    impl FakeHasher {
        fn new() -> Self {
            Self { seen: RefCell::new(vec![]) }
        }
    }
    impl Hasher for FakeHasher {
        fn sha256(&self, data: &[u8]) -> [u8; 32] {
            self.seen.borrow_mut().push(data.to_vec());
            let mut out = [0u8; 32];
            for (i, b) in out.iter_mut().enumerate() {
                *b = (data.len() as u8).wrapping_add(i as u8);
            }
            out
        }
    }

    struct FakeSigner {
        signed: RefCell<Vec<Vec<u8>>>,
        fail: bool,
    }
    impl FakeSigner {
        fn new() -> Self {
            Self { signed: RefCell::new(vec![]), fail: false }
        }
    }
    impl ProofSigner for FakeSigner {
        fn sign_es256(&self, input: &[u8]) -> Result<[u8; 64], SignError> {
            if self.fail {
                return Err(SignError("no key"));
            }
            self.signed.borrow_mut().push(input.to_vec());
            Ok([7u8; 64])
        }
        fn public_xy(&self) -> ([u8; 32], [u8; 32]) {
            ([1u8; 32], [2u8; 32])
        }
    }

    const HTU: &str = "https://api.shikshacom.com/api/sessions/collaboration/v1/device-token/";

    fn proof(token: Option<&str>) -> (String, FakeSigner) {
        let (s, h) = (FakeSigner::new(), FakeHasher::new());
        let p = build_proof(
            &s,
            &h,
            &mut CountingEntropy::default(),
            &ProofRequest { method: "post", htu: HTU, iat: 1_700_000_000, access_token: token },
        )
        .unwrap();
        (p, s)
    }

    #[test]
    fn jwk_is_the_rfc7638_canonical_form() {
        let j = jwk_json(&[0u8; 32], &[0xffu8; 32]);
        assert_eq!(
            j,
            format!(
                r#"{{"crv":"P-256","kty":"EC","x":"{}","y":"{}"}}"#,
                "A".repeat(43),
                "_".repeat(42) + "8"
            )
        );
        assert!(!j.contains(' '));
    }

    #[test]
    fn thumbprint_hashes_exactly_the_canonical_jwk() {
        let h = FakeHasher::new();
        let _ = thumbprint(&h, &[1u8; 32], &[2u8; 32]);
        assert_eq!(h.seen.borrow().len(), 1);
        assert_eq!(h.seen.borrow()[0], jwk_json(&[1u8; 32], &[2u8; 32]).into_bytes());
    }

    #[test]
    fn ath_hashes_the_access_token_bytes() {
        let h = FakeHasher::new();
        let _ = access_token_hash(&h, "tok.en");
        assert_eq!(h.seen.borrow()[0], b"tok.en".to_vec());
    }

    #[test]
    fn proof_is_three_segments_and_the_signature_covers_exactly_the_first_two() {
        let (p, s) = proof(None);
        let parts: Vec<&str> = p.split('.').collect();
        assert_eq!(parts.len(), 3);
        let signed = s.signed.borrow();
        assert_eq!(signed.len(), 1);
        assert_eq!(signed[0], format!("{}.{}", parts[0], parts[1]).into_bytes());
        assert_eq!(parts[2], b64url(&[7u8; 64]), "r||s, 64 bytes, not DER");
    }

    #[test]
    fn header_and_payload_have_the_profile_and_nothing_else() {
        let (p, _) = proof(None);
        let parts: Vec<&str> = p.split('.').collect();
        let header = format!(
            r#"{{"alg":"ES256","typ":"dpop+jwt","jwk":{}}}"#,
            jwk_json(&[1u8; 32], &[2u8; 32])
        );
        assert_eq!(parts[0], b64url(header.as_bytes()));
        let jti = b64url(&CountingEntropy::default().bytes16());
        let payload = format!(
            r#"{{"htm":"POST","htu":"{HTU}","iat":1700000000,"jti":"{jti}"}}"#
        );
        assert_eq!(parts[1], b64url(payload.as_bytes()));
        assert!(!payload.contains("ath"), "no ath on an unauthenticated route");
        assert_eq!(jti.len(), 22);
    }

    #[test]
    fn a_resource_request_adds_ath_of_the_token() {
        let (p, _) = proof(Some("access.token"));
        let parts: Vec<&str> = p.split('.').collect();
        let h = FakeHasher::new();
        let ath = access_token_hash(&h, "access.token");
        let jti = b64url(&CountingEntropy::default().bytes16());
        let payload = format!(
            r#"{{"htm":"POST","htu":"{HTU}","iat":1700000000,"jti":"{jti}","ath":"{ath}"}}"#
        );
        assert_eq!(parts[1], b64url(payload.as_bytes()));
    }

    #[test]
    fn every_proof_gets_a_fresh_jti() {
        let (s, h) = (FakeSigner::new(), FakeHasher::new());
        let mut en = CountingEntropy::default();
        let req = ProofRequest { method: "POST", htu: HTU, iat: 1, access_token: None };
        let a = build_proof(&s, &h, &mut en, &req).unwrap();
        let b = build_proof(&s, &h, &mut en, &req).unwrap();
        assert_ne!(a, b);
    }

    #[test]
    fn htu_must_be_absolute_https_without_query_or_fragment() {
        for bad in [
            "http://api.shikshacom.com/x",
            "https://api.shikshacom.com/x?y=1",
            "https://api.shikshacom.com/x#f",
            "api.shikshacom.com/x",
            "https://",
            "https:///path",
            "https://a b/x",
            "ftp://a/x",
            "",
        ] {
            assert_eq!(check_htu(bad), Err(DpopError::BadHtu), "{bad:?}");
        }
        assert!(check_htu("https://api.shikshacom.com/a/b/").is_ok());
        assert!(check_htu("http://localhost:8000/a/").is_ok());
        assert!(check_htu("http://127.0.0.1/a/").is_ok());
        assert_eq!(check_htu("http://localhost.evil.com/a/"), Err(DpopError::BadHtu));
    }

    #[test]
    fn htu_for_joins_like_the_server_does() {
        assert_eq!(
            htu_for("https://api.shikshacom.com/api/sessions/collaboration/v1/", "/pairings/").unwrap(),
            "https://api.shikshacom.com/api/sessions/collaboration/v1/pairings/"
        );
        assert_eq!(
            htu_for("https://x/v1", "grants/abc/lease/").unwrap(),
            "https://x/v1/grants/abc/lease/"
        );
        assert_eq!(htu_for("https://x/v1", "a?b=1"), Err(DpopError::BadHtu));
    }

    #[test]
    fn bad_method_and_signer_failure_and_bad_htu_are_errors_not_unsigned_proofs() {
        let (s, h) = (FakeSigner::new(), FakeHasher::new());
        let mut en = CountingEntropy::default();
        let bad_method = ProofRequest { method: "PO ST", htu: HTU, iat: 1, access_token: None };
        assert_eq!(build_proof(&s, &h, &mut en, &bad_method), Err(DpopError::BadMethod));
        let bad_htu = ProofRequest { method: "POST", htu: "https://x/y?z", iat: 1, access_token: None };
        assert_eq!(build_proof(&s, &h, &mut en, &bad_htu), Err(DpopError::BadHtu));
        let failing = FakeSigner { signed: RefCell::new(vec![]), fail: true };
        let ok = ProofRequest { method: "POST", htu: HTU, iat: 1, access_token: None };
        assert_eq!(
            build_proof(&failing, &h, &mut en, &ok),
            Err(DpopError::Sign(SignError("no key")))
        );
    }

    #[test]
    fn clock_offset_shifts_iat_only() {
        let off = ClockOffset::learn(1_000_100, 1_000_000);
        assert_eq!(off.seconds(), 100);
        assert_eq!(off.apply(1_000_000), 1_000_100);
        let behind = ClockOffset::learn(1_000_000, 1_000_300);
        assert_eq!(behind.apply(1_000_300), 1_000_000);
        assert_eq!(ClockOffset::default().apply(5), 5);
    }
}
