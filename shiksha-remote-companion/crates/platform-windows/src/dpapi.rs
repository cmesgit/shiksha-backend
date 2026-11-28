//! A [`SecretStore`] that keeps each secret as a DPAPI blob in a file.
//!
//! ## What this protects, and what it does not
//!
//! DPAPI (`CryptProtectData`) encrypts under a key derived from the **signed-in
//! Windows user's** credentials. So the files are useless to another user, to an
//! offline reader of the disk, and to a copy of the profile moved to another
//! machine. That is the "OS-protected storage" the architecture asks for, and it
//! is a real improvement over plaintext.
//!
//! 🔴 It is **not** protection against other code running as the same user: any
//! such process can call `CryptUnprotectData` on these files. The per-secret
//! entropy below is a constant, not a secret — it binds a blob to its *name* (so
//! a blob cannot be swapped between names) and is not an access control.
//! Closing that gap needs a non-exportable key in the TPM (CNG with the Platform
//! Crypto Provider), so the private key never enters process memory at all. That
//! is a stronger design and is **not done**; this store is the floor, not the
//! ceiling. See `evidence/companion-core.md`.
//!
//! ## Guarantees the callers rely on
//!
//! * `get` returns `Ok(None)` **only** when the file does not exist. A file that
//!   is present but will not decrypt is an `Err`: a caller that saw `None` would
//!   create a fresh device key and orphan the pairing.
//! * `put` is durable on return: the data is written to a temporary file in the
//!   same directory, flushed to disk, then renamed over the target. A crash
//!   leaves either the old value or the new one, never a torn file. (Windows
//!   offers no portable directory sync through `std`, so the rename's own
//!   durability is the OS's; the file contents are synced.)
//! * No fallback location. If `%LOCALAPPDATA%` is missing the store refuses to
//!   open rather than writing to the working directory or a temp folder.

use std::path::{Path, PathBuf};

use shiksha_companion_core::secrets::{valid_name, SecretStore, StoreError};

/// Prefix of the optional entropy handed to DPAPI. The secret's name is appended.
/// Used by the real (Windows) path and exercised directly by tests on every
/// platform; the `cfg(not(windows))` stub below never touches it.
#[cfg(any(windows, test))]
const ENTROPY_PREFIX: &[u8] = b"shiksha-remote-companion/v1/";
const EXTENSION: &str = "dpapi";

// ── Raw DPAPI ─────────────────────────────────────────────────────────────

#[cfg(windows)]
mod sys {
    use std::ffi::c_void;

    /// `DATA_BLOB` / `CRYPT_INTEGER_BLOB`.
    #[repr(C)]
    pub struct DataBlob {
        pub cb_data: u32,
        pub pb_data: *mut u8,
    }

    #[link(name = "crypt32")]
    extern "system" {
        pub fn CryptProtectData(
            p_data_in: *const DataBlob,
            sz_data_descr: *const u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *const c_void,
            p_prompt_struct: *const c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;

        pub fn CryptUnprotectData(
            p_data_in: *const DataBlob,
            ppsz_data_descr: *mut *mut u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *const c_void,
            p_prompt_struct: *const c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        pub fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
    }

    /// Never show a UI prompt. A companion has no business popping a dialog from
    /// a crypto call, and a hidden prompt would hang a background thread.
    pub const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;
}

/// Entropy bytes binding a blob to a secret name.
#[cfg(any(windows, test))]
fn entropy_for(name: &str) -> Vec<u8> {
    let mut e = Vec::with_capacity(ENTROPY_PREFIX.len() + name.len());
    e.extend_from_slice(ENTROPY_PREFIX);
    e.extend_from_slice(name.as_bytes());
    e
}

/// Overwrite with zeros in a way the optimiser may not remove.
#[cfg(any(windows, test))]
fn wipe(buf: &mut [u8]) {
    for b in buf.iter_mut() {
        // SAFETY: `b` is a valid, aligned, exclusive reference to a u8.
        unsafe { std::ptr::write_volatile(b, 0) };
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

#[cfg(windows)]
fn blob_of(data: &[u8]) -> Result<sys::DataBlob, StoreError> {
    let len = u32::try_from(data.len()).map_err(|_| StoreError("too_large"))?;
    // DPAPI only reads through this pointer for input blobs; the cast to *mut is
    // the API's declaration, not a licence to write.
    Ok(sys::DataBlob { cb_data: len, pb_data: data.as_ptr() as *mut u8 })
}

/// Copy a DPAPI-allocated output blob into a `Vec`, zero it, and `LocalFree` it.
///
/// # Safety
/// `out` must be a blob DPAPI just filled in (valid `pb_data`/`cb_data`, allocated
/// with `LocalAlloc`), and must not be used again afterwards.
#[cfg(windows)]
unsafe fn take_output(out: sys::DataBlob) -> Vec<u8> {
    if out.pb_data.is_null() {
        return Vec::new();
    }
    let slice = std::slice::from_raw_parts_mut(out.pb_data, out.cb_data as usize);
    let copy = slice.to_vec();
    wipe(slice);
    sys::LocalFree(out.pb_data as *mut std::ffi::c_void);
    copy
}

/// Encrypt `plain` under the current user, bound to `name`.
#[cfg(windows)]
pub fn protect(plain: &[u8], name: &str) -> Result<Vec<u8>, StoreError> {
    let entropy = entropy_for(name);
    let input = blob_of(plain)?;
    let ent = blob_of(&entropy)?;
    let mut out = sys::DataBlob { cb_data: 0, pb_data: std::ptr::null_mut() };
    // SAFETY: all pointers are valid for the call; `out` is written by DPAPI and
    // consumed exactly once by `take_output`.
    let ok = unsafe {
        sys::CryptProtectData(
            &input,
            std::ptr::null(),
            &ent,
            std::ptr::null(),
            std::ptr::null(),
            sys::CRYPTPROTECT_UI_FORBIDDEN,
            &mut out,
        )
    };
    if ok == 0 {
        return Err(StoreError("protect_failed"));
    }
    // SAFETY: DPAPI succeeded, so `out` is a LocalAlloc'd blob.
    Ok(unsafe { take_output(out) })
}

/// Decrypt a blob made by [`protect`] with the same `name`, as the same user.
#[cfg(windows)]
pub fn unprotect(blob: &[u8], name: &str) -> Result<Vec<u8>, StoreError> {
    let entropy = entropy_for(name);
    let input = blob_of(blob)?;
    let ent = blob_of(&entropy)?;
    let mut out = sys::DataBlob { cb_data: 0, pb_data: std::ptr::null_mut() };
    // SAFETY: as in `protect`. The description out-parameter is passed null,
    // which the API permits, so there is nothing extra to free.
    let ok = unsafe {
        sys::CryptUnprotectData(
            &input,
            std::ptr::null_mut(),
            &ent,
            std::ptr::null(),
            std::ptr::null(),
            sys::CRYPTPROTECT_UI_FORBIDDEN,
            &mut out,
        )
    };
    if ok == 0 {
        // Wrong user, wrong machine, wrong name, or a damaged file: all one
        // answer on purpose. Never `None`.
        return Err(StoreError("unprotect_failed"));
    }
    // SAFETY: DPAPI succeeded, so `out` is a LocalAlloc'd blob.
    Ok(unsafe { take_output(out) })
}

#[cfg(not(windows))]
pub fn protect(_plain: &[u8], _name: &str) -> Result<Vec<u8>, StoreError> {
    Err(StoreError("unsupported_platform"))
}

#[cfg(not(windows))]
pub fn unprotect(_blob: &[u8], _name: &str) -> Result<Vec<u8>, StoreError> {
    Err(StoreError("unsupported_platform"))
}

// ── The store ─────────────────────────────────────────────────────────────

pub struct DpapiStore {
    dir: PathBuf,
}

impl DpapiStore {
    /// Use `dir`, creating it if needed.
    pub fn open(dir: impl Into<PathBuf>) -> Result<Self, StoreError> {
        let dir = dir.into();
        std::fs::create_dir_all(&dir).map_err(|_| StoreError("create_dir_failed"))?;
        Ok(Self { dir })
    }

    /// `%LOCALAPPDATA%\ShikshaRemote\secrets`. There is deliberately **no
    /// fallback**: a missing or empty variable is an error, never the current
    /// directory or a temp folder.
    pub fn open_default() -> Result<Self, StoreError> {
        Self::open(Self::default_dir()?)
    }

    pub fn default_dir() -> Result<PathBuf, StoreError> {
        match std::env::var_os("LOCALAPPDATA") {
            Some(v) if !v.is_empty() => {
                Ok(Path::new(&v).join("ShikshaRemote").join("secrets"))
            }
            _ => Err(StoreError("no_local_app_data")),
        }
    }

    fn path_for(&self, name: &str) -> Result<PathBuf, StoreError> {
        if !valid_name(name) {
            return Err(StoreError("invalid_name"));
        }
        Ok(self.dir.join(format!("{name}.{EXTENSION}")))
    }

    fn temp_path_for(&self, name: &str) -> PathBuf {
        // Same directory, so the final rename never crosses a volume.
        self.dir.join(format!("{name}.{EXTENSION}.tmp"))
    }
}

impl SecretStore for DpapiStore {
    fn get(&self, name: &str) -> Result<Option<Vec<u8>>, StoreError> {
        let path = self.path_for(name)?;
        match std::fs::read(&path) {
            Ok(blob) => unprotect(&blob, name).map(Some),
            // Only "no such file" means absent. A permissions error or a sharing
            // violation is an error: absent would trigger a new key.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(StoreError("read_failed")),
        }
    }

    fn put(&self, name: &str, value: &[u8]) -> Result<(), StoreError> {
        use std::io::Write;
        let path = self.path_for(name)?;
        let tmp = self.temp_path_for(name);
        let blob = protect(value, name)?;

        let write = || -> std::io::Result<()> {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(&blob)?;
            // Contents on disk before the rename makes them the live value.
            f.sync_all()?;
            drop(f);
            // Replaces an existing target atomically on Windows.
            std::fs::rename(&tmp, &path)
        };
        match write() {
            Ok(()) => Ok(()),
            Err(_) => {
                let _ = std::fs::remove_file(&tmp);
                Err(StoreError("write_failed"))
            }
        }
    }

    fn delete(&self, name: &str) -> Result<(), StoreError> {
        let path = self.path_for(name)?;
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(StoreError("delete_failed")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A unique scratch directory under the system temp dir, removed on drop.
    struct Scratch(PathBuf);
    impl Scratch {
        fn new(tag: &str) -> Self {
            let n = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let p = std::env::temp_dir().join(format!("shiksha-dpapi-test-{tag}-{n}"));
            Scratch(p)
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn entropy_binds_a_blob_to_its_name() {
        assert_ne!(entropy_for("device-key-v1"), entropy_for("refresh-token-v1"));
        assert!(entropy_for("x").starts_with(ENTROPY_PREFIX));
    }

    #[test]
    fn wipe_zeroes_every_byte() {
        let mut b = vec![0xAAu8; 64];
        wipe(&mut b);
        assert!(b.iter().all(|&x| x == 0));
    }

    #[test]
    fn invalid_names_never_reach_the_file_system() {
        let s = Scratch::new("names");
        let store = DpapiStore::open(&s.0).unwrap();
        for bad in ["../escape", "a/b", "a\\b", "", "UPPER", "dot.name"] {
            assert_eq!(store.get(bad), Err(StoreError("invalid_name")), "{bad:?}");
            assert_eq!(store.put(bad, b"v"), Err(StoreError("invalid_name")), "{bad:?}");
            assert_eq!(store.delete(bad), Err(StoreError("invalid_name")), "{bad:?}");
        }
        // Nothing was created outside, or even inside, the store directory.
        assert_eq!(std::fs::read_dir(&s.0).unwrap().count(), 0);
    }

    #[test]
    fn an_absent_secret_is_none_and_deleting_it_is_fine() {
        let s = Scratch::new("absent");
        let store = DpapiStore::open(&s.0).unwrap();
        assert_eq!(store.get("device-key-v1"), Ok(None));
        assert_eq!(store.delete("device-key-v1"), Ok(()));
    }

    #[test]
    fn default_dir_refuses_to_fall_back() {
        // Only meaningful where the variable can be unset safely; the check is on
        // the function's contract: it must never return a path without a base.
        match DpapiStore::default_dir() {
            Ok(p) => assert!(p.ends_with("ShikshaRemote/secrets") || p.ends_with("ShikshaRemote\\secrets")),
            Err(e) => assert_eq!(e, StoreError("no_local_app_data")),
        }
    }

    #[cfg(windows)]
    mod windows_only {
        use super::*;

        #[test]
        fn protect_then_unprotect_round_trips_and_the_blob_is_not_the_plaintext() {
            let plain = b"super secret refresh token";
            let blob = protect(plain, "refresh-token-v1").unwrap();
            assert_ne!(blob, plain.to_vec());
            assert!(!blob.windows(plain.len()).any(|w| w == plain), "plaintext must not appear");
            assert_eq!(unprotect(&blob, "refresh-token-v1").unwrap(), plain.to_vec());
        }

        #[test]
        fn a_blob_cannot_be_decrypted_under_another_name() {
            let blob = protect(b"v", "device-key-v1").unwrap();
            assert_eq!(unprotect(&blob, "refresh-token-v1"), Err(StoreError("unprotect_failed")));
        }

        #[test]
        fn empty_and_large_values_round_trip() {
            let empty = protect(b"", "k").unwrap();
            assert_eq!(unprotect(&empty, "k").unwrap(), Vec::<u8>::new());
            let big = vec![7u8; 100_000];
            assert_eq!(unprotect(&protect(&big, "k").unwrap(), "k").unwrap(), big);
        }

        #[test]
        fn the_store_round_trips_overwrites_and_leaves_no_temp_file() {
            let s = Scratch::new("rt");
            let store = DpapiStore::open(&s.0).unwrap();
            store.put("refresh-token-v1", b"one").unwrap();
            assert_eq!(store.get("refresh-token-v1"), Ok(Some(b"one".to_vec())));
            store.put("refresh-token-v1", b"two").unwrap();
            assert_eq!(store.get("refresh-token-v1"), Ok(Some(b"two".to_vec())));
            let names: Vec<_> = std::fs::read_dir(&s.0)
                .unwrap()
                .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            assert_eq!(names, vec!["refresh-token-v1.dpapi".to_string()], "no .tmp left behind");
        }

        #[test]
        fn the_file_on_disk_is_not_the_plaintext() {
            let s = Scratch::new("disk");
            let store = DpapiStore::open(&s.0).unwrap();
            store.put("device-key-v1", b"PLAINTEXT-MARKER-0123456789").unwrap();
            let raw = std::fs::read(s.0.join("device-key-v1.dpapi")).unwrap();
            assert!(!raw.windows(10).any(|w| w == b"PLAINTEXT-".as_slice()));
        }

        #[test]
        fn a_corrupt_file_is_an_error_never_none() {
            // `None` would make a caller mint a new device key over a present one.
            let s = Scratch::new("corrupt");
            let store = DpapiStore::open(&s.0).unwrap();
            store.put("device-key-v1", b"v").unwrap();
            std::fs::write(s.0.join("device-key-v1.dpapi"), b"not a dpapi blob").unwrap();
            assert_eq!(store.get("device-key-v1"), Err(StoreError("unprotect_failed")));
        }

        #[test]
        fn a_file_copied_under_another_name_will_not_decrypt() {
            let s = Scratch::new("swap");
            let store = DpapiStore::open(&s.0).unwrap();
            store.put("device-key-v1", b"the key").unwrap();
            std::fs::copy(s.0.join("device-key-v1.dpapi"), s.0.join("refresh-token-v1.dpapi")).unwrap();
            assert!(store.get("refresh-token-v1").is_err(), "a swapped blob must not be accepted");
        }
    }
}
