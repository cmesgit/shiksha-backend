//! The device key: P-256 for DPoP, SHA-256, and where the key lives.
//!
//! Persistence goes through `core::secrets::SecretStore`. The Windows
//! implementation (DPAPI) is `shiksha-companion-platform-windows`; `MemoryStore`
//! is for tests. Never add a plain-file store: a device key readable from a file
//! defeats the point of binding tokens to it.

use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
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

pub use shiksha_companion_core::secrets::{
    MemoryStore, SecretStore, StoreError, DEVICE_ID_NAME, KEY_NAME, REFRESH_INFLIGHT_NAME,
    REFRESH_NAME,
};

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
