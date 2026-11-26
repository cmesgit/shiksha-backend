//! The device key: P-256 for DPoP, SHA-256, and where the key lives.
//!
//! 🔴 **Persistence is not finished, and that is release-gating.** The
//! architecture requires the key in *OS-protected storage* (DPAPI/TPM on
//! Windows). This module defines [`SecretStore`] and a [`MemoryStore`] for tests
//! and development; **no OS-protected implementation exists yet.** Do not add a
//! plain-file store to fill the gap: a device key readable by any process of the
//! same user defeats the point of binding tokens to it (a stolen access token is
//! meant to be useless without the key).

use std::collections::BTreeMap;
use std::sync::Mutex;

use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use sha2::{Digest, Sha256};
use shiksha_companion_core::dpop::{Hasher, ProofSigner, SignError};

pub struct Sha256Hasher;

impl Hasher for Sha256Hasher {
    fn sha256(&self, data: &[u8]) -> [u8; 32] {
        Sha256::digest(data).into()
    }
}

pub struct P256Key {
    key: SigningKey,
    x: [u8; 32],
    y: [u8; 32],
}

#[derive(Debug, PartialEq, Eq)]
pub enum KeyError {
    /// The bytes are not a valid P-256 scalar.
    Invalid,
    /// The OS random source failed.
    Random,
}

impl P256Key {
    pub fn from_secret_bytes(secret: &[u8]) -> Result<Self, KeyError> {
        let key = SigningKey::from_slice(secret).map_err(|_| KeyError::Invalid)?;
        let point = key.verifying_key().to_encoded_point(false);
        let (x, y) = match (point.x(), point.y()) {
            (Some(x), Some(y)) => (x, y),
            _ => return Err(KeyError::Invalid),
        };
        let (mut xa, mut ya) = ([0u8; 32], [0u8; 32]);
        xa.copy_from_slice(x);
        ya.copy_from_slice(y);
        Ok(Self { key, x: xa, y: ya })
    }

    /// A new random key. The scalar is drawn from the OS CSPRNG and rejected and
    /// redrawn if it falls outside the curve order (probability ~2^-32).
    pub fn generate() -> Result<Self, KeyError> {
        for _ in 0..8 {
            let mut b = [0u8; 32];
            getrandom::getrandom(&mut b).map_err(|_| KeyError::Random)?;
            if let Ok(k) = Self::from_secret_bytes(&b) {
                return Ok(k);
            }
        }
        Err(KeyError::Random)
    }

    /// The private scalar, for handing to a [`SecretStore`]. Treat as a secret.
    pub fn secret_bytes(&self) -> Vec<u8> {
        self.key.to_bytes().to_vec()
    }
}

impl ProofSigner for P256Key {
    fn sign_es256(&self, signing_input: &[u8]) -> Result<[u8; 64], SignError> {
        // `Signature::to_bytes` is the fixed-size r||s form JWS requires, not
        // DER. The Signer impl hashes with SHA-256, which is what ES256 means.
        let sig: Signature = self.key.sign(signing_input);
        let mut out = [0u8; 64];
        out.copy_from_slice(&sig.to_bytes());
        Ok(out)
    }
    fn public_xy(&self) -> ([u8; 32], [u8; 32]) {
        (self.x, self.y)
    }
}

impl std::fmt::Debug for P256Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("P256Key(<redacted>)")
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct StoreError(pub &'static str);

/// Protected storage for small secrets (device key, refresh token, the
/// refresh-in-flight marker). An implementation must encrypt at rest under the
/// OS user's credentials. [`MemoryStore`] does not and is for tests only.
pub trait SecretStore {
    fn get(&self, name: &str) -> Result<Option<Vec<u8>>, StoreError>;
    fn put(&self, name: &str, value: &[u8]) -> Result<(), StoreError>;
    fn delete(&self, name: &str) -> Result<(), StoreError>;
}

pub const KEY_NAME: &str = "device-key-v1";
pub const REFRESH_NAME: &str = "refresh-token-v1";
pub const DEVICE_ID_NAME: &str = "device-id-v1";
/// Written *before* a refresh request is sent and deleted once its outcome is
/// known, so a crash mid-refresh is recognisable at the next start.
pub const REFRESH_INFLIGHT_NAME: &str = "refresh-inflight-v1";

/// Load the device key, creating and saving one on first run. A stored value
/// that is not a valid key is an error, never silently replaced: replacing it
/// would orphan the pairing.
pub fn load_or_create(store: &dyn SecretStore) -> Result<P256Key, LoadError> {
    match store.get(KEY_NAME).map_err(LoadError::Store)? {
        Some(bytes) => P256Key::from_secret_bytes(&bytes).map_err(LoadError::Key),
        None => {
            let key = P256Key::generate().map_err(LoadError::Key)?;
            store.put(KEY_NAME, &key.secret_bytes()).map_err(LoadError::Store)?;
            Ok(key)
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum LoadError {
    Store(StoreError),
    Key(KeyError),
}

#[derive(Default)]
pub struct MemoryStore {
    inner: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl SecretStore for MemoryStore {
    fn get(&self, name: &str) -> Result<Option<Vec<u8>>, StoreError> {
        Ok(self.inner.lock().map_err(|_| StoreError("poisoned"))?.get(name).cloned())
    }
    fn put(&self, name: &str, value: &[u8]) -> Result<(), StoreError> {
        self.inner.lock().map_err(|_| StoreError("poisoned"))?.insert(name.to_string(), value.to_vec());
        Ok(())
    }
    fn delete(&self, name: &str) -> Result<(), StoreError> {
        self.inner.lock().map_err(|_| StoreError("poisoned"))?.remove(name);
        Ok(())
    }
}
