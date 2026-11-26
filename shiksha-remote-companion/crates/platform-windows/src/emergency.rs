//! The physical emergency Stop: a global hotkey that closes the gate.
//!
//! Requirements (`companion-architecture.md`, `security-test-plan.md`):
//! * a physical chord, **Ctrl+Alt+Shift+F12** by default, that works while another
//!   program — including a full-screen one — has focus;
//! * it acts on the **stop latch** directly, with no server round trip and no lock
//!   on the companion, so it works offline and while the rest of the app is busy
//!   or wedged ([`StopLatch::stop`] is a single atomic add);
//! * "Failure to register an effective emergency control blocks activation": a
//!   [`EmergencyStop::register`] that returns `Err` must leave
//!   `Companion::set_emergency_stop_ready(false)`, which the core enforces;
//! * the chord is never injectable by the protocol (the core's `prohibited_chord`
//!   refuses Ctrl+Alt+Shift+F12 as a resulting pressed set), so the controller
//!   cannot trigger the owner's Stop either. Stopping would be harmless, but the
//!   owner's control should not be something the remote side can play with.
//!
//! 🔴 A system-tray "Quit" is not a substitute (security-test-plan.md): the Stop
//! must be reachable by a physical chord *and* a visible button. The button (the
//! native status window) is not written yet.
//!
//! The registration thread owns a message loop; `RegisterHotKey` delivers
//! `WM_HOTKEY` to the thread that registered, so registering from elsewhere would
//! silently never fire. It reports back whether registration succeeded.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use shiksha_companion_core::companion::StopLatch;

pub const MOD_ALT: u32 = 0x0001;
pub const MOD_CONTROL: u32 = 0x0002;
pub const MOD_SHIFT: u32 = 0x0004;
/// Holding the chord must not fire repeatedly.
pub const MOD_NOREPEAT: u32 = 0x4000;
pub const VK_F12: u32 = 0x7B;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct Chord {
    pub mods: u32,
    pub vk: u32,
}

pub const DEFAULT_CHORD: Chord = Chord { mods: MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, vk: VK_F12 };

/// How the chord is shown to the owner, verbatim, in the consent copy and the
/// status window. It must match [`DEFAULT_CHORD`]; a test pins that.
pub const DEFAULT_STOP_SHORTCUT: &str = "Ctrl+Alt+Shift+F12";

/// Human text for a chord (modifiers in a fixed order, then the key).
pub fn describe(c: &Chord) -> String {
    let mut parts: Vec<String> = Vec::new();
    if c.mods & MOD_CONTROL != 0 {
        parts.push("Ctrl".into());
    }
    if c.mods & MOD_ALT != 0 {
        parts.push("Alt".into());
    }
    if c.mods & MOD_SHIFT != 0 {
        parts.push("Shift".into());
    }
    // VK_F1..VK_F24 are 0x70..0x87.
    if (0x70..=0x87).contains(&c.vk) {
        parts.push(format!("F{}", c.vk - 0x6F));
    } else {
        parts.push(format!("0x{:02X}", c.vk));
    }
    parts.join("+")
}

#[derive(Debug, PartialEq, Eq)]
pub enum HotkeyError {
    /// Another program already owns the chord. Control is not offered.
    AlreadyRegistered,
    Failed(u32),
    ThreadFailed,
}

#[cfg(windows)]
pub use win::EmergencyStop;

#[cfg(windows)]
mod win {
    use super::*;

    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    struct Msg {
        hwnd: isize,
        message: u32,
        w_param: usize,
        l_param: isize,
        time: u32,
        pt: Point,
    }

    const _: () = assert!(std::mem::size_of::<Msg>() == if cfg!(target_pointer_width = "64") { 48 } else { 28 });

    #[link(name = "user32")]
    extern "system" {
        fn RegisterHotKey(hwnd: isize, id: i32, modifiers: u32, vk: u32) -> i32;
        fn UnregisterHotKey(hwnd: isize, id: i32) -> i32;
        fn GetMessageW(msg: *mut Msg, hwnd: isize, min: u32, max: u32) -> i32;
        fn PostThreadMessageW(thread: u32, msg: u32, w: usize, l: isize) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThreadId() -> u32;
        fn GetLastError() -> u32;
    }

    const WM_HOTKEY: u32 = 0x0312;
    const WM_QUIT: u32 = 0x0012;
    const ERROR_HOTKEY_ALREADY_REGISTERED: u32 = 1409;
    const HOTKEY_ID: i32 = 0x5348; // "SH"

    /// While this exists the chord is registered. Dropping it unregisters.
    pub struct EmergencyStop {
        thread_id: u32,
        fired: Arc<AtomicBool>,
        join: Option<std::thread::JoinHandle<()>>,
        chord: Chord,
    }

    impl EmergencyStop {
        /// Register `chord` and make it close `latch`. `Err` means there is **no
        /// effective emergency Stop**, and activation must stay blocked.
        pub fn register(latch: Arc<StopLatch>, chord: Chord) -> Result<Self, HotkeyError> {
            let fired = Arc::new(AtomicBool::new(false));
            let fired_t = fired.clone();
            let (tx, rx) = std::sync::mpsc::channel::<Result<u32, HotkeyError>>();
            let join = std::thread::Builder::new()
                .name("emergency-stop".into())
                .spawn(move || {
                    // SAFETY: registered and pumped on this one thread, as
                    // WM_HOTKEY requires.
                    unsafe {
                        if RegisterHotKey(0, HOTKEY_ID, chord.mods, chord.vk) == 0 {
                            let err = GetLastError();
                            let _ = tx.send(Err(if err == ERROR_HOTKEY_ALREADY_REGISTERED {
                                HotkeyError::AlreadyRegistered
                            } else {
                                HotkeyError::Failed(err)
                            }));
                            return;
                        }
                        let _ = tx.send(Ok(GetCurrentThreadId()));
                        let mut msg: Msg = std::mem::zeroed();
                        while GetMessageW(&mut msg, 0, 0, 0) > 0 {
                            if msg.message == WM_HOTKEY && msg.w_param == HOTKEY_ID as usize {
                                // Close the gate FIRST, lock-free, before anything
                                // else can observe it open.
                                latch.stop();
                                fired_t.store(true, Ordering::SeqCst);
                            }
                        }
                        UnregisterHotKey(0, HOTKEY_ID);
                    }
                })
                .map_err(|_| HotkeyError::ThreadFailed)?;
            match rx.recv() {
                Ok(Ok(thread_id)) => Ok(Self { thread_id, fired, join: Some(join), chord }),
                Ok(Err(e)) => {
                    let _ = join.join();
                    Err(e)
                }
                Err(_) => Err(HotkeyError::ThreadFailed),
            }
        }

        pub fn register_default(latch: Arc<StopLatch>) -> Result<Self, HotkeyError> {
            Self::register(latch, DEFAULT_CHORD)
        }

        /// Has the owner pressed the chord since the last call? The latch is already
        /// closed; this tells the app to run the companion's cleanup
        /// (`Companion::sync_after_latch_stop`) and tell the controller.
        pub fn take_fired(&self) -> bool {
            self.fired.swap(false, Ordering::SeqCst)
        }

        pub fn shortcut_text(&self) -> String {
            describe(&self.chord)
        }
    }

    impl Drop for EmergencyStop {
        fn drop(&mut self) {
            // SAFETY: posting WM_QUIT to our own message-loop thread.
            unsafe {
                PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0);
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
        }
    }
}

// ── compile-time checks ───────────────────────────────────────────────────

const _: () = assert!(DEFAULT_CHORD.vk == 0x7B, "F12");
const _: () = assert!(DEFAULT_CHORD.mods & MOD_CONTROL != 0 && DEFAULT_CHORD.mods & MOD_ALT != 0 && DEFAULT_CHORD.mods & MOD_SHIFT != 0);
const _: () = assert!(DEFAULT_CHORD.mods & MOD_NOREPEAT != 0, "holding the chord must not fire repeatedly");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_chord_is_described_exactly_as_the_owner_is_told() {
        assert_eq!(describe(&DEFAULT_CHORD), DEFAULT_STOP_SHORTCUT);
    }

    #[test]
    fn other_chords_describe_sensibly() {
        assert_eq!(describe(&Chord { mods: MOD_CONTROL | MOD_SHIFT, vk: 0x70 }), "Ctrl+Shift+F1");
        assert_eq!(describe(&Chord { mods: MOD_ALT, vk: 0x87 }), "Alt+F24");
        assert_eq!(describe(&Chord { mods: 0, vk: 0x41 }), "0x41");
    }

    #[test]
    fn the_modifier_flag_values_are_the_win32_ones() {
        // RegisterHotKey's documented values. A wrong one registers a different
        // chord and the owner's Stop silently does not work.
        assert_eq!((MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT), (1, 2, 4, 0x4000));
    }

    #[test]
    fn the_chord_shown_to_the_owner_is_the_chord_the_core_refuses_to_inject() {
        // The core's `prohibited_chord` blocks Ctrl+Alt+Shift+F12 as a resulting
        // pressed set. If the default ever changes, that rule must change with it;
        // this test is the reminder that they are one decision.
        assert_eq!(DEFAULT_STOP_SHORTCUT, "Ctrl+Alt+Shift+F12");
        assert!(shiksha_companion_core::wire::is_allowed_key("F12"), "F12 is injectable alone, only the full chord is not");
    }
}
