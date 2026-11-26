//! Composition root.
//!
//! What this does today: builds the core with a real monotonic clock and OS
//! entropy, starts the watchdog tick on its own thread, and sits idle. It joins
//! no room (core's `may_join` is false until the owner physically approves
//! sharing), holds no network connection, and refuses every OS call because the
//! only adapter wired in is [`RefusingAdapter`].
//!
//! What it deliberately does not do yet: LiveKit join, DPoP device API client,
//! credential refresh, key storage, native consent UI and the Windows
//! capture/input adapter. The first two belong to the remainder of RC-012;
//! the rest are RC-013 and RC-014. See HANDOFF, RC-012 section.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use shiksha_companion_core::adapter::{AdapterError, InputAdapter, Release, ValidatedEvent};
use shiksha_companion_core::clock::SystemClock;
use shiksha_companion_core::companion::{Companion, DeviceIdentity, StopLatch};
use shiksha_companion_transport::entropy::OsEntropy;

/// Stand-in until the Windows adapter (RC-013) exists. Failing closed is the
/// point: a build that forgets to wire a real adapter injects nothing.
struct RefusingAdapter;

impl InputAdapter for RefusingAdapter {
    fn supports_key(&self, _code: &str) -> bool {
        false
    }
    fn inject(&mut self, _event: &ValidatedEvent) -> Result<(), AdapterError> {
        Err(AdapterError("no_os_adapter_until_rc013"))
    }
    fn release(&mut self, _what: &Release) -> Result<(), AdapterError> {
        Ok(())
    }
}

/// How often the watchdog ticks. The tick is what enforces lease, heartbeat and
/// hold deadlines, so it must not share a thread with capture or network work.
const WATCHDOG_PERIOD: Duration = Duration::from_millis(50);

fn main() {
    let latch = StopLatch::new();
    let companion = Companion::new(
        RefusingAdapter,
        SystemClock::new(),
        OsEntropy,
        latch.clone(),
        // Real values come from the pairing flow (RC-012 remainder).
        DeviceIdentity { device_id: String::new(), key_thumbprint: String::new() },
    );
    let companion = Arc::new(Mutex::new(companion));

    let watchdog = {
        let companion = companion.clone();
        thread::Builder::new()
            .name("watchdog".into())
            .spawn(move || loop {
                thread::sleep(WATCHDOG_PERIOD);
                // A poisoned lock means a panic happened mid-decision. Do not
                // continue on state that may be half-updated: close the gate.
                match companion.lock() {
                    Ok(mut c) => {
                        let _ = c.tick();
                        let _ = c.pump();
                    }
                    Err(poisoned) => {
                        let mut c = poisoned.into_inner();
                        let _ = c.local_stop();
                    }
                }
            })
            .expect("spawn watchdog")
    };

    println!(
        "shiksha-companion idle: no room joined, no network, no OS input adapter (RC-012 core only)"
    );
    // The latch is what a Stop hotkey or tray button calls, lock-free.
    let _stop_handle = latch;
    let _ = watchdog.join();
}
