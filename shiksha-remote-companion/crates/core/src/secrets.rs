//! Where small secrets live: the device key, the refresh token, and the
//! refresh-in-flight marker.
//!
//! Only the *interface* is here, so the crates that need a store (transport) and
//! the crate that implements one for an OS (platform-windows) can both depend on
//! core without depending on each other.
//!
//! 🔴 An implementation must encrypt at rest under the OS user's credentials.
//! [`MemoryStore`] does not persist at all and exists for tests. **Never add an
//! implementation that writes plaintext to disk**: the device key is what makes a
//! stolen access token useless, and a key readable from a file defeats that.

use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoreError(pub &'static str);

pub trait SecretStore {
    /// `Ok(None)` means *definitely absent*. A value that exists but cannot be
    /// read or decrypted must be `Err`, never `None`: callers create a new key
    /// when they see `None`, and doing that over a corrupt-but-present key would
    /// orphan the pairing.
    fn get(&self, name: &str) -> Result<Option<Vec<u8>>, StoreError>;

    /// Durable on return: when this returns `Ok`, the value survives a crash.
    /// The refresh-rotation policy depends on it (the rotated token must be saved
    /// before it is used for anything).
    fn put(&self, name: &str, value: &[u8]) -> Result<(), StoreError>;

    /// Idempotent: deleting an absent name is `Ok`.
    fn delete(&self, name: &str) -> Result<(), StoreError>;
}

pub const KEY_NAME: &str = "device-key-v1";
pub const REFRESH_NAME: &str = "refresh-token-v1";
pub const DEVICE_ID_NAME: &str = "device-id-v1";
/// Written *before* a refresh request is sent and deleted once its outcome is
/// known, so a crash mid-refresh is recognisable at the next start.
pub const REFRESH_INFLIGHT_NAME: &str = "refresh-inflight-v1";

/// Names are restricted so they can become file names without any escaping or
/// traversal question: lowercase ASCII letters, digits and `-`, 1..=64 chars.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

#[derive(Default)]
pub struct MemoryStore {
    inner: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl SecretStore for MemoryStore {
    fn get(&self, name: &str) -> Result<Option<Vec<u8>>, StoreError> {
        if !valid_name(name) {
            return Err(StoreError("invalid_name"));
        }
        Ok(self.inner.lock().map_err(|_| StoreError("poisoned"))?.get(name).cloned())
    }
    fn put(&self, name: &str, value: &[u8]) -> Result<(), StoreError> {
        if !valid_name(name) {
            return Err(StoreError("invalid_name"));
        }
        self.inner.lock().map_err(|_| StoreError("poisoned"))?.insert(name.to_string(), value.to_vec());
        Ok(())
    }
    fn delete(&self, name: &str) -> Result<(), StoreError> {
        if !valid_name(name) {
            return Err(StoreError("invalid_name"));
        }
        self.inner.lock().map_err(|_| StoreError("poisoned"))?.remove(name);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_safe_to_use_as_file_names() {
        for good in ["device-key-v1", "a", "refresh-token-v1", &"a".repeat(64)] {
            assert!(valid_name(good), "{good}");
        }
        for bad in [
            "", "A", "has space", "../x", "a/b", "a\\b", "a.b", "con:", "x\0", "ünï", &"a".repeat(65),
            "a_b",
        ] {
            assert!(!valid_name(bad), "{bad:?}");
        }
    }

    #[test]
    fn the_well_known_names_are_valid() {
        for n in [KEY_NAME, REFRESH_NAME, DEVICE_ID_NAME, REFRESH_INFLIGHT_NAME] {
            assert!(valid_name(n), "{n}");
        }
    }

    #[test]
    fn memory_store_round_trips_overwrites_and_deletes_idempotently() {
        let s = MemoryStore::default();
        assert_eq!(s.get("k"), Ok(None));
        s.put("k", b"one").unwrap();
        assert_eq!(s.get("k"), Ok(Some(b"one".to_vec())));
        s.put("k", b"two").unwrap();
        assert_eq!(s.get("k"), Ok(Some(b"two".to_vec())));
        s.delete("k").unwrap();
        s.delete("k").unwrap();
        assert_eq!(s.get("k"), Ok(None));
    }

    #[test]
    fn an_invalid_name_is_an_error_not_an_absent_value() {
        let s = MemoryStore::default();
        assert_eq!(s.get("../x"), Err(StoreError("invalid_name")));
        assert_eq!(s.put("../x", b"v"), Err(StoreError("invalid_name")));
    }
}
