//! OS CSPRNG behind core's `Entropy` trait.

use shiksha_companion_core::clock::Entropy;

pub struct OsEntropy;

impl Entropy for OsEntropy {
    fn bytes16(&mut self) -> [u8; 16] {
        let mut b = [0u8; 16];
        // If the OS cannot produce randomness there is no safe fallback: a
        // predictable challenge defeats the freshness check. Refuse to run.
        getrandom::getrandom(&mut b).expect("OS random source unavailable");
        b
    }
}
