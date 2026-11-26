//! Compact-JWT verification for activation tickets and leases (EdDSA only).
//!
//! The server side is `sessions_app/collaboration/leases.py`. Order of checks
//! here is the order that fails closed cheapest: shape, header `alg`, `kid`
//! lookup in the **pinned** key set, then the signature. A token whose header
//! names any other algorithm never reaches a signature routine, which is what
//! stops the `alg: none` / HS256-with-the-public-key downgrades.
//!
//! Claim *meaning* (audience, expiry, capabilities, device, room…) is checked by
//! `shiksha_companion_core::ticket`; this module only proves who signed what and
//! turns JSON into typed claims.

use std::collections::BTreeMap;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::Value;
use sha2::{Digest, Sha256};
use shiksha_companion_core::clock::b64url;
use shiksha_companion_core::ticket::{ActivationClaims, LeaseClaims, VerifiedHeader, ALGORITHM};

use crate::codec::parse_strict;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum JwtError {
    Shape,
    Header,
    Algorithm,
    UnknownKey,
    Signature,
    Claims,
}

/// Pinned public keys by `kid`. There is no way to add one from a token.
#[derive(Default)]
pub struct TrustRoot {
    keys: BTreeMap<String, VerifyingKey>,
}

impl TrustRoot {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn pin(&mut self, kid: &str, public_key: [u8; 32]) -> Result<(), JwtError> {
        let vk = VerifyingKey::from_bytes(&public_key).map_err(|_| JwtError::Header)?;
        self.keys.insert(kid.to_string(), vk);
        Ok(())
    }
    pub fn kids(&self) -> Vec<String> {
        self.keys.keys().cloned().collect()
    }
}

pub struct Verified {
    pub header: VerifiedHeader,
    pub payload: Value,
    /// base64url(SHA-256(token)): what every lease must echo as
    /// `activation_hash`. Only meaningful for an activation ticket.
    pub token_hash: String,
}

pub fn verify(token: &str, root: &TrustRoot) -> Result<Verified, JwtError> {
    let mut parts = token.split('.');
    let (h, p, s) = match (parts.next(), parts.next(), parts.next(), parts.next()) {
        (Some(h), Some(p), Some(s), None) if !h.is_empty() && !p.is_empty() && !s.is_empty() => {
            (h, p, s)
        }
        _ => return Err(JwtError::Shape),
    };

    let header_bytes = URL_SAFE_NO_PAD.decode(h).map_err(|_| JwtError::Header)?;
    let header = parse_strict(&header_bytes).map_err(|_| JwtError::Header)?;
    let get = |k: &str| header.get(k).and_then(Value::as_str).map(str::to_owned);
    let (alg, typ, kid) = match (get("alg"), get("typ"), get("kid")) {
        (Some(a), Some(t), Some(k)) => (a, t, k),
        _ => return Err(JwtError::Header),
    };
    if alg != ALGORITHM {
        return Err(JwtError::Algorithm);
    }
    let key = root.keys.get(&kid).ok_or(JwtError::UnknownKey)?;

    let sig_bytes = URL_SAFE_NO_PAD.decode(s).map_err(|_| JwtError::Signature)?;
    let sig = Signature::from_slice(&sig_bytes).map_err(|_| JwtError::Signature)?;
    // `verify_strict` also rejects small-order keys and non-canonical signatures.
    let signing_input = &token[..h.len() + 1 + p.len()];
    key.verify_strict(signing_input.as_bytes(), &sig).map_err(|_| JwtError::Signature)?;

    let payload_bytes = URL_SAFE_NO_PAD.decode(p).map_err(|_| JwtError::Claims)?;
    let payload = parse_strict(&payload_bytes).map_err(|_| JwtError::Claims)?;
    if !payload.is_object() {
        return Err(JwtError::Claims);
    }
    Ok(Verified {
        header: VerifiedHeader { alg, typ, kid },
        payload,
        token_hash: b64url(&Sha256::digest(token.as_bytes())),
    })
}

fn s(v: &Value, k: &str) -> Result<String, JwtError> {
    v.get(k).and_then(Value::as_str).map(str::to_owned).ok_or(JwtError::Claims)
}
fn u(v: &Value, k: &str) -> Result<u64, JwtError> {
    v.get(k).and_then(Value::as_u64).ok_or(JwtError::Claims)
}
fn i(v: &Value, k: &str) -> Result<i64, JwtError> {
    v.get(k).and_then(Value::as_i64).ok_or(JwtError::Claims)
}

pub fn activation_claims(payload: &Value) -> Result<ActivationClaims, JwtError> {
    let caps = payload
        .get("capabilities")
        .and_then(Value::as_array)
        .ok_or(JwtError::Claims)?
        .iter()
        .map(|c| c.as_str().map(str::to_owned).ok_or(JwtError::Claims))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ActivationClaims {
        aud: s(payload, "aud")?,
        protocol: u(payload, "protocol")?,
        grant_id: s(payload, "grant_id")?,
        grant_version: u(payload, "grant_version")?,
        epoch: s(payload, "epoch")?,
        room_name: s(payload, "room_name")?,
        controller_identity: s(payload, "controller_identity")?,
        controller_sid: s(payload, "controller_sid")?,
        companion_identity: s(payload, "companion_identity")?,
        companion_sid: s(payload, "companion_sid")?,
        device_id: s(payload, "device_id")?,
        device_thumbprint: s(payload, "device_thumbprint")?,
        host_binding_id: s(payload, "host_binding_id")?,
        capabilities: caps,
        capture_generation: u(payload, "capture_generation")?,
        track_sid: s(payload, "track_sid")?,
        display_id: s(payload, "display_id")?,
        nbf: i(payload, "nbf")?,
        exp: i(payload, "exp")?,
    })
}

pub fn lease_claims(payload: &Value) -> Result<LeaseClaims, JwtError> {
    Ok(LeaseClaims {
        aud: s(payload, "aud")?,
        protocol: u(payload, "protocol")?,
        grant_id: s(payload, "grant_id")?,
        grant_version: u(payload, "grant_version")?,
        epoch: s(payload, "epoch")?,
        device_id: s(payload, "device_id")?,
        lease_seq: u(payload, "lease_seq")?,
        nonce: s(payload, "nonce")?,
        // Absent on a lease minted without a ticket; the core then fails the
        // activation-hash comparison, which is the safe direction.
        activation_hash: payload
            .get("activation_hash")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        iat: i(payload, "iat")?,
        exp: i(payload, "exp")?,
    })
}
