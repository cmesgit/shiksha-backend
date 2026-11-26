//! Who produced this input: the owner, or us?
//!
//! Two requirements meet here.
//!
//! 1. **Native consent dialogs must not be answerable by remote input**
//!    (T13). The OS marks every event that came through `SendInput` (ours or
//!    anyone's) as *injected*; a physical device's event is not. A click on a
//!    consent dialog is genuine only if the click was not injected.
//! 2. **Local input priority**: when the owner physically touches the keyboard
//!    or mouse during a grant, remote injection pauses and remote-owned states
//!    are released ([`TransportEvent::PhysicalActivity`]).
//!
//! 🔴 This is a low-level keyboard/mouse hook, which is exactly what a key logger
//! is built from, so the design is about **not being one**:
//! * injected events are ignored outright;
//! * no key value, character or position is stored. The only per-key state kept
//!   is whether each of the **eight modifier keys** is physically held (a bitmask),
//!   needed so ending a session does not cancel a Shift the owner is pressing.
//!   Every other key is reduced to "some activity happened";
//! * the hook runs **only while a grant can inject** (started at activation,
//!   stopped at its end), never at idle. A companion with a permanent global hook
//!   is the "hidden monitoring" the threat model forbids, and some antivirus
//!   products flag it regardless.
//!
//! The hook procedure must return quickly or Windows silently removes the hook
//! (`LowLevelHooksTimeout`), so it only updates atomics. Nothing is called back into
//! the core from the hook thread; the app polls [`PhysicalState::take_activity`].
//!
//! Where provenance cannot be established the answer is to keep the Stop controls
//! and *disable* automatic pausing rather than guess (companion-architecture.md).

use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering};

use crate::keymap;

// ── Pure classification (cross-platform, testable) ────────────────────────

pub const LLKHF_EXTENDED: u32 = 0x01;
pub const LLKHF_INJECTED: u32 = 0x10;
pub const LLKHF_UP: u32 = 0x80;
pub const LLMHF_INJECTED: u32 = 0x01;

/// Pixels the pointer must move before it counts as the owner using the mouse.
/// A resting hand and sensor noise produce tiny movements; without a deadzone the
/// grant would end every time the table is bumped.
pub const MOVE_DEADZONE_PX: i32 = 8;

pub const fn kbd_injected(flags: u32) -> bool {
    flags & LLKHF_INJECTED != 0
}
pub const fn mouse_injected(flags: u32) -> bool {
    flags & LLMHF_INJECTED != 0
}
pub const fn kbd_is_up(flags: u32) -> bool {
    flags & LLKHF_UP != 0
}

/// Scan-code identity as [`keymap`] uses it: low byte plus `0xE000` if extended.
pub const fn scan_id(scan_code: u32, flags: u32) -> u16 {
    let base = (scan_code & 0xFF) as u16;
    if flags & LLKHF_EXTENDED != 0 {
        0xE000 | base
    } else {
        base
    }
}

/// Bit for each modifier key, by scan identity. The *only* keys whose held state
/// is ever remembered.
pub const fn modifier_bit(id: u16) -> Option<u8> {
    match id {
        0x2A => Some(0),   // ShiftLeft
        0x36 => Some(1),   // ShiftRight
        0x1D => Some(2),   // ControlLeft
        0xE01D => Some(3), // ControlRight
        0x38 => Some(4),   // AltLeft
        0xE038 => Some(5), // AltRight
        0xE05B => Some(6), // MetaLeft
        0xE05C => Some(7), // MetaRight
        _ => None,
    }
}

pub fn beyond_deadzone(prev: (i32, i32), now: (i32, i32)) -> bool {
    (now.0 - prev.0).abs() > MOVE_DEADZONE_PX || (now.1 - prev.1).abs() > MOVE_DEADZONE_PX
}

/// Shared between the hook thread (writer) and the app (reader). Atomics only.
#[derive(Default)]
pub struct PhysicalState {
    /// Bitmask of physically-held modifiers.
    mods: AtomicU32,
    /// Set by any physical activity; cleared by [`Self::take_activity`].
    activity: AtomicBool,
    last_x: AtomicI32,
    last_y: AtomicI32,
    have_pos: AtomicBool,
}

impl PhysicalState {
    pub fn new() -> Self {
        Self::default()
    }

    /// A keyboard event. Injected events change nothing.
    pub fn on_key(&self, scan_code: u32, flags: u32) {
        if kbd_injected(flags) {
            return;
        }
        let id = scan_id(scan_code, flags);
        if let Some(bit) = modifier_bit(id) {
            if kbd_is_up(flags) {
                self.mods.fetch_and(!(1u32 << bit), Ordering::SeqCst);
            } else {
                self.mods.fetch_or(1u32 << bit, Ordering::SeqCst);
            }
        }
        // Any physical key, modifier or not, is activity. Nothing else is kept.
        self.activity.store(true, Ordering::SeqCst);
    }

    /// A mouse button or wheel event.
    pub fn on_mouse_other(&self, flags: u32) {
        if mouse_injected(flags) {
            return;
        }
        self.activity.store(true, Ordering::SeqCst);
    }

    /// A mouse move. Counts only beyond the deadzone from the last *physical*
    /// position; the position is held only to measure that distance.
    pub fn on_mouse_move(&self, x: i32, y: i32, flags: u32) {
        if mouse_injected(flags) {
            return;
        }
        if !self.have_pos.swap(true, Ordering::SeqCst) {
            self.last_x.store(x, Ordering::SeqCst);
            self.last_y.store(y, Ordering::SeqCst);
            return; // the first sample is a baseline, not activity
        }
        let prev = (self.last_x.load(Ordering::SeqCst), self.last_y.load(Ordering::SeqCst));
        if beyond_deadzone(prev, (x, y)) {
            self.last_x.store(x, Ordering::SeqCst);
            self.last_y.store(y, Ordering::SeqCst);
            self.activity.store(true, Ordering::SeqCst);
        }
    }

    /// Has the owner physically used the machine since the last call? Clears it.
    pub fn take_activity(&self) -> bool {
        self.activity.swap(false, Ordering::SeqCst)
    }

    /// Is this DOM key a modifier the owner is physically holding right now?
    /// Non-modifier keys always answer `false`: their state is never recorded.
    pub fn modifier_held(&self, dom_code: &str) -> bool {
        match keymap::lookup(dom_code).and_then(modifier_bit) {
            Some(bit) => self.mods.load(Ordering::SeqCst) & (1u32 << bit) != 0,
            None => false,
        }
    }

    /// Forget everything. Called when the hook stops.
    pub fn reset(&self) {
        self.mods.store(0, Ordering::SeqCst);
        self.activity.store(false, Ordering::SeqCst);
        self.have_pos.store(false, Ordering::SeqCst);
    }
}

/// Is a click on a native consent control genuine? Only a non-injected one.
/// (The dialog itself is RC-014; this is the rule it must apply.)
pub const fn click_is_physical(mouse_flags: u32) -> bool {
    !mouse_injected(mouse_flags)
}

// ── The hook (Windows) ────────────────────────────────────────────────────

#[cfg(windows)]
pub use hook::{MonitorError, PhysicalMonitor};

#[cfg(windows)]
mod hook {
    use super::PhysicalState;
    use std::sync::{Arc, RwLock};

    const WH_KEYBOARD_LL: i32 = 13;
    const WH_MOUSE_LL: i32 = 14;
    const WM_QUIT: u32 = 0x0012;
    const WM_MOUSEMOVE: usize = 0x0200;

    #[repr(C)]
    struct KbdLlHookStruct {
        vk_code: u32,
        scan_code: u32,
        flags: u32,
        time: u32,
        dw_extra_info: usize,
    }

    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    struct MsLlHookStruct {
        pt: Point,
        mouse_data: u32,
        flags: u32,
        time: u32,
        dw_extra_info: usize,
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

    const _: () = assert!(std::mem::size_of::<KbdLlHookStruct>() == if cfg!(target_pointer_width = "64") { 24 } else { 20 });
    const _: () = assert!(std::mem::size_of::<MsLlHookStruct>() == if cfg!(target_pointer_width = "64") { 32 } else { 24 });

    type HookProc = unsafe extern "system" fn(i32, usize, isize) -> isize;

    #[link(name = "user32")]
    extern "system" {
        fn SetWindowsHookExW(id: i32, proc_: HookProc, module: isize, thread: u32) -> isize;
        fn UnhookWindowsHookEx(hook: isize) -> i32;
        fn CallNextHookEx(hook: isize, code: i32, w: usize, l: isize) -> isize;
        fn GetMessageW(msg: *mut Msg, hwnd: isize, min: u32, max: u32) -> i32;
        fn PostThreadMessageW(thread: u32, msg: u32, w: usize, l: isize) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThreadId() -> u32;
    }

    /// The hook procedures are plain `extern "system" fn`s with no closure state,
    /// so the shared state lives in a static. It is set before the hooks exist and
    /// cleared after they are gone, so a monitor can be started again for the next
    /// grant (a set-once static would allow exactly one grant per process).
    static STATE: RwLock<Option<Arc<PhysicalState>>> = RwLock::new(None);

    unsafe extern "system" fn kbd_proc(code: i32, w: usize, l: isize) -> isize {
        if code >= 0 {
            // `try_read`: a hook must never block. Losing one event while the monitor
            // is being torn down is harmless.
            if let Ok(g) = STATE.try_read() {
                if let Some(st) = g.as_ref() {
                    // SAFETY: for WH_KEYBOARD_LL, `l` points to a KBDLLHOOKSTRUCT
                    // that is valid for the duration of this call.
                    let k = &*(l as *const KbdLlHookStruct);
                    st.on_key(k.scan_code, k.flags);
                }
            }
        }
        CallNextHookEx(0, code, w, l)
    }

    unsafe extern "system" fn mouse_proc(code: i32, w: usize, l: isize) -> isize {
        if code >= 0 {
            if let Ok(g) = STATE.try_read() {
                if let Some(st) = g.as_ref() {
                    // SAFETY: for WH_MOUSE_LL, `l` points to an MSLLHOOKSTRUCT
                    // valid for the duration of this call.
                    let m = &*(l as *const MsLlHookStruct);
                    if w == WM_MOUSEMOVE {
                        st.on_mouse_move(m.pt.x, m.pt.y, m.flags);
                    } else {
                        st.on_mouse_other(m.flags);
                    }
                }
            }
        }
        CallNextHookEx(0, code, w, l)
    }

    #[derive(Debug, PartialEq, Eq)]
    pub enum MonitorError {
        /// A monitor is already running in this process.
        AlreadyRunning,
        HookFailed,
    }

    /// While this value exists the hooks are installed; dropping it removes them
    /// and forgets all state. Create one when a grant becomes able to inject and
    /// drop it when the grant ends.
    pub struct PhysicalMonitor {
        state: Arc<PhysicalState>,
        thread_id: u32,
        join: Option<std::thread::JoinHandle<()>>,
    }

    impl PhysicalMonitor {
        pub fn start() -> Result<Self, MonitorError> {
            Self::start_with(Arc::new(PhysicalState::new()))
        }

        /// Start feeding an existing state. The input adapter reads held modifiers
        /// from a state that is built before any grant exists, so each monitor must
        /// write into that same one. Stopping the monitor resets it.
        pub fn start_with(state: Arc<PhysicalState>) -> Result<Self, MonitorError> {
            {
                let mut slot = STATE.write().map_err(|_| MonitorError::HookFailed)?;
                if slot.is_some() {
                    // One monitor at a time: two would share one static.
                    return Err(MonitorError::AlreadyRunning);
                }
                *slot = Some(state.clone());
            }
            // Any failure below must give the slot back, or no monitor could ever
            // start again.
            let release = || {
                if let Ok(mut slot) = STATE.write() {
                    *slot = None;
                }
            };
            let (tx, rx) = std::sync::mpsc::channel::<Result<u32, ()>>();
            let join = std::thread::Builder::new()
                .name("physical-input-monitor".into())
                .spawn(move || {
                    // SAFETY: hooks are installed and removed on this thread, which
                    // also pumps messages, as low-level hooks require.
                    unsafe {
                        let kb = SetWindowsHookExW(WH_KEYBOARD_LL, kbd_proc, 0, 0);
                        let ms = SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, 0, 0);
                        if kb == 0 || ms == 0 {
                            if kb != 0 {
                                UnhookWindowsHookEx(kb);
                            }
                            if ms != 0 {
                                UnhookWindowsHookEx(ms);
                            }
                            let _ = tx.send(Err(()));
                            return;
                        }
                        let _ = tx.send(Ok(GetCurrentThreadId()));
                        let mut msg: Msg = std::mem::zeroed();
                        // Returns 0 on WM_QUIT, -1 on error.
                        while GetMessageW(&mut msg, 0, 0, 0) > 0 {}
                        UnhookWindowsHookEx(kb);
                        UnhookWindowsHookEx(ms);
                    }
                })
                .map_err(|_| {
                    release();
                    MonitorError::HookFailed
                })?;
            match rx.recv() {
                Ok(Ok(thread_id)) => Ok(Self { state, thread_id, join: Some(join) }),
                _ => {
                    let _ = join.join();
                    release();
                    Err(MonitorError::HookFailed)
                }
            }
        }

        pub fn state(&self) -> Arc<PhysicalState> {
            self.state.clone()
        }
    }

    impl Drop for PhysicalMonitor {
        fn drop(&mut self) {
            // SAFETY: posting WM_QUIT to our own message-loop thread.
            unsafe {
                PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0);
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
            // Hooks are gone; now clear the static and forget every key state.
            if let Ok(mut slot) = STATE.write() {
                *slot = None;
            }
            self.state.reset();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHIFT_L: u32 = 0x2A;
    const KEY_A: u32 = 0x1E;

    #[test]
    fn injected_events_are_ignored_entirely() {
        let s = PhysicalState::new();
        s.on_key(SHIFT_L, LLKHF_INJECTED);
        s.on_key(KEY_A, LLKHF_INJECTED);
        s.on_mouse_other(LLMHF_INJECTED);
        s.on_mouse_move(0, 0, LLMHF_INJECTED);
        s.on_mouse_move(500, 500, LLMHF_INJECTED);
        assert!(!s.take_activity(), "remote input must never look like the owner");
        assert!(!s.modifier_held("ShiftLeft"));
    }

    #[test]
    fn physical_events_are_activity_and_the_same_calls_prove_it() {
        // The positive twin of the test above.
        let s = PhysicalState::new();
        s.on_key(KEY_A, 0);
        assert!(s.take_activity());
        assert!(!s.take_activity(), "cleared by taking it");
        s.on_mouse_other(0);
        assert!(s.take_activity());
    }

    #[test]
    fn only_modifier_state_is_remembered_and_only_as_held_or_not() {
        let s = PhysicalState::new();
        s.on_key(SHIFT_L, 0);
        assert!(s.modifier_held("ShiftLeft"));
        assert!(!s.modifier_held("ShiftRight"));
        assert!(!s.modifier_held("KeyA"), "ordinary keys are never recorded");
        s.on_key(KEY_A, 0);
        assert!(!s.modifier_held("KeyA"));
        s.on_key(SHIFT_L, LLKHF_UP);
        assert!(!s.modifier_held("ShiftLeft"));
    }

    #[test]
    fn extended_modifiers_are_distinct_from_their_left_twins() {
        let s = PhysicalState::new();
        s.on_key(0x1D, LLKHF_EXTENDED); // right control
        assert!(s.modifier_held("ControlRight"));
        assert!(!s.modifier_held("ControlLeft"));
        s.on_key(0x38, LLKHF_EXTENDED); // right alt (AltGr)
        assert!(s.modifier_held("AltRight"));
        assert!(!s.modifier_held("AltLeft"));
        s.on_key(0x5B, LLKHF_EXTENDED); // left windows
        assert!(s.modifier_held("MetaLeft"));
    }

    #[test]
    fn all_eight_modifiers_have_distinct_bits_and_nothing_else_does() {
        let mods = ["ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "MetaLeft", "MetaRight"];
        let mut seen = 0u32;
        for m in mods {
            let bit = keymap::lookup(m).and_then(modifier_bit).unwrap_or_else(|| panic!("{m}"));
            assert_eq!(seen & (1 << bit), 0, "{m} reuses a bit");
            seen |= 1 << bit;
        }
        assert_eq!(seen, 0xFF);
        for (code, _) in keymap::KEYMAP {
            if !mods.contains(&code) {
                assert_eq!(keymap::lookup(code).and_then(modifier_bit), None, "{code}");
            }
        }
    }

    #[test]
    fn small_mouse_movement_is_not_activity_but_a_real_move_is() {
        let s = PhysicalState::new();
        s.on_mouse_move(100, 100, 0); // baseline
        assert!(!s.take_activity(), "the first sample is only a baseline");
        s.on_mouse_move(104, 103, 0);
        assert!(!s.take_activity(), "sensor noise");
        s.on_mouse_move(100 + MOVE_DEADZONE_PX + 1, 100, 0);
        assert!(s.take_activity());
    }

    #[test]
    fn a_remote_move_cannot_disturb_the_physical_baseline() {
        let s = PhysicalState::new();
        s.on_mouse_move(100, 100, 0);
        s.on_mouse_move(900, 900, LLMHF_INJECTED); // the remote controller moving it
        s.on_mouse_move(103, 101, 0);
        assert!(!s.take_activity(), "measured from the last PHYSICAL position");
    }

    #[test]
    fn reset_forgets_everything() {
        let s = PhysicalState::new();
        s.on_key(SHIFT_L, 0);
        s.on_mouse_move(1, 1, 0);
        s.reset();
        assert!(!s.modifier_held("ShiftLeft"));
        assert!(!s.take_activity());
        s.on_mouse_move(500, 500, 0);
        assert!(!s.take_activity(), "baseline again after a reset");
    }

    #[test]
    fn a_consent_click_counts_only_if_it_was_not_injected() {
        assert!(click_is_physical(0));
        assert!(!click_is_physical(LLMHF_INJECTED));
    }

    #[test]
    fn scan_identity_matches_the_key_map_encoding() {
        assert_eq!(scan_id(0x48, 0), keymap::lookup("Numpad8").unwrap());
        assert_eq!(scan_id(0x48, LLKHF_EXTENDED), keymap::lookup("ArrowUp").unwrap());
    }
}
