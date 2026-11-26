//! Time sources.
//!
//! Every *authority* deadline in this crate is a [`Mono`] reading. Wall-clock
//! time appears in exactly one place: translating a ticket's `exp` into a
//! monotonic deadline once, at activation. After that a clock jump, an NTP
//! step or a user changing the date cannot extend or shorten anything.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

/// Milliseconds on a monotonic clock. Not comparable across processes.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Mono(pub u64);

impl Mono {
    pub fn plus_ms(self, ms: u64) -> Mono {
        Mono(self.0.saturating_add(ms))
    }
    /// Milliseconds elapsed since `earlier`; zero if `earlier` is in the future.
    pub fn since(self, earlier: Mono) -> u64 {
        self.0.saturating_sub(earlier.0)
    }
}

pub trait MonotonicClock {
    fn now(&self) -> Mono;
}

/// Real clock: `Instant`, which does not go backwards on Windows or Unix.
pub struct SystemClock {
    start: Instant,
}

impl SystemClock {
    pub fn new() -> Self {
        Self { start: Instant::now() }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl MonotonicClock for SystemClock {
    fn now(&self) -> Mono {
        Mono(self.start.elapsed().as_millis() as u64)
    }
}

/// Test clock. Only moves when told to.
#[derive(Default)]
pub struct FakeClock {
    ms: AtomicU64,
}

impl FakeClock {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn advance(&self, ms: u64) {
        self.ms.fetch_add(ms, Ordering::SeqCst);
    }
}

impl MonotonicClock for FakeClock {
    fn now(&self) -> Mono {
        Mono(self.ms.load(Ordering::SeqCst))
    }
}

/// Source of unpredictable bytes for nonces and challenges.
///
/// The real implementation (transport crate) reads the OS CSPRNG. It is a trait
/// so that core stays dependency-free, **not** so that anything may supply a
/// weak one: a predictable challenge defeats the freshness check.
pub trait Entropy {
    fn bytes16(&mut self) -> [u8; 16];
}

/// Deterministic, **insecure** entropy for tests only.
#[derive(Default)]
pub struct CountingEntropy {
    n: u64,
}

impl Entropy for CountingEntropy {
    fn bytes16(&mut self) -> [u8; 16] {
        self.n += 1;
        let mut out = [0u8; 16];
        out[..8].copy_from_slice(&self.n.to_be_bytes());
        out[8..].copy_from_slice(&(!self.n).to_be_bytes());
        out
    }
}

const B64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// Unpadded base64url. 16 bytes encode to the 22 characters the contract's
/// `challenge` pattern requires.
pub fn b64url(bytes: &[u8]) -> String {
    let mut out = String::with_capacity((bytes.len() * 4).div_ceil(3));
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64URL[(n >> 18) as usize & 63] as char);
        out.push(B64URL[(n >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(B64URL[(n >> 6) as usize & 63] as char);
        }
        if chunk.len() > 2 {
            out.push(B64URL[n as usize & 63] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sixteen_bytes_make_a_contract_challenge() {
        assert_eq!(b64url(&[0u8; 16]), "AAAAAAAAAAAAAAAAAAAAAA");
        assert_eq!(b64url(&[0xff; 16]).len(), 22);
    }

    #[test]
    fn rfc4648_vectors_unpadded() {
        assert_eq!(b64url(b""), "");
        assert_eq!(b64url(b"f"), "Zg");
        assert_eq!(b64url(b"fo"), "Zm8");
        assert_eq!(b64url(b"foo"), "Zm9v");
        assert_eq!(b64url(&[0xfb, 0xff]), "-_8");
    }

    #[test]
    fn since_never_underflows() {
        assert_eq!(Mono(5).since(Mono(9)), 0);
    }
}
