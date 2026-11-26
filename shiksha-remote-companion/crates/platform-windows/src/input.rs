//! The Windows input adapter: turns a gate-approved [`ValidatedEvent`] into
//! `SendInput` calls.
//!
//! Everything reaching this module has already passed the core's gate. What is
//! left is making sure it lands where and how the contract says:
//!
//! * pointer position is applied **in the same `SendInput` call** as the button or
//!   wheel it belongs to, so nothing can be inserted between "move here" and
//!   "click";
//! * keys are sent as **scan codes** (`KEYEVENTF_SCANCODE`), i.e. physical
//!   positions — never virtual-key integers from the network;
//! * text is sent as `KEYEVENTF_UNICODE` UTF-16 units, with LF and TAB turned into
//!   the Enter and Tab *keys* because a Unicode `\n` is not what an editor expects;
//! * every event carries [`OWN_EXTRA_INFO`], so our own events are recognisable;
//! * after an absolute move the cursor is read back and a position error beyond the
//!   2-pixel acceptance budget is a **reported failure**, not a silent drift.
//!
//! 🔴 `INPUT` must be exactly 40 bytes on x64 (28 on x86). RC-003's first probe
//! declared two extra fields (56 bytes): `SendInput` returned 0 with
//! `ERROR_INVALID_PARAMETER`, every sample "failed", and it looked like an
//! environment problem. The size is now a compile-time assertion.
//!
//! ## Things this cannot do, said plainly
//! * **UIPI**: injecting into a window of a *higher* integrity level (an elevated
//!   app, a UAC prompt) is silently dropped by Windows while `SendInput` still
//!   reports success. That is not detected here; it is expected-not-controllable
//!   and needs a foreground-integrity check before it can be a clear message.
//! * **Secure desktop / lock screen**: `SendInput` fails with access denied. That is
//!   mapped to `input_blocked`, which ends the grant.

use shiksha_companion_core::adapter::{
    Action, AdapterError, InputAdapter, Release, ValidatedEvent,
};
use shiksha_companion_core::wire::Button;

use crate::display::{absolute_point, desktop_point, Bounds, DisplayDescriptor};
use crate::keymap::{scan_for, Scan};

// ── Win32 structures ──────────────────────────────────────────────────────

pub const INPUT_MOUSE: u32 = 0;
pub const INPUT_KEYBOARD: u32 = 1;

pub const MOUSEEVENTF_MOVE: u32 = 0x0001;
pub const MOUSEEVENTF_LEFTDOWN: u32 = 0x0002;
pub const MOUSEEVENTF_LEFTUP: u32 = 0x0004;
pub const MOUSEEVENTF_RIGHTDOWN: u32 = 0x0008;
pub const MOUSEEVENTF_RIGHTUP: u32 = 0x0010;
pub const MOUSEEVENTF_MIDDLEDOWN: u32 = 0x0020;
pub const MOUSEEVENTF_MIDDLEUP: u32 = 0x0040;
pub const MOUSEEVENTF_WHEEL: u32 = 0x0800;
pub const MOUSEEVENTF_HWHEEL: u32 = 0x1000;
pub const MOUSEEVENTF_VIRTUALDESK: u32 = 0x4000;
pub const MOUSEEVENTF_ABSOLUTE: u32 = 0x8000;

pub const KEYEVENTF_EXTENDEDKEY: u32 = 0x0001;
pub const KEYEVENTF_KEYUP: u32 = 0x0002;
pub const KEYEVENTF_UNICODE: u32 = 0x0004;
pub const KEYEVENTF_SCANCODE: u32 = 0x0008;

/// `WHEEL_DELTA`: one notch.
pub const WHEEL_DELTA: i32 = 120;

/// Tag on every event we send ("SHKR"). The OS already marks injected input, so
/// this is only for our own bookkeeping and for anyone debugging a trace.
pub const OWN_EXTRA_INFO: usize = 0x5348_4B52;

#[repr(C)]
#[derive(Copy, Clone)]
pub struct MouseInput {
    pub dx: i32,
    pub dy: i32,
    pub mouse_data: u32,
    pub dw_flags: u32,
    pub time: u32,
    pub dw_extra_info: usize,
}

#[repr(C)]
#[derive(Copy, Clone)]
pub struct KeybdInput {
    pub w_vk: u16,
    pub w_scan: u16,
    pub dw_flags: u32,
    pub time: u32,
    pub dw_extra_info: usize,
}

#[repr(C)]
#[derive(Copy, Clone)]
pub union InputUnion {
    pub mi: MouseInput,
    pub ki: KeybdInput,
}

#[repr(C)]
#[derive(Copy, Clone)]
pub struct Input {
    pub type_: u32,
    pub u: InputUnion,
}

// 🔴 The size SendInput checks. Wrong => returns 0 with ERROR_INVALID_PARAMETER.
const _: () = assert!(
    std::mem::size_of::<Input>() == if cfg!(target_pointer_width = "64") { 40 } else { 28 },
    "INPUT has the wrong size; SendInput will reject every call"
);
const _: () = assert!(std::mem::size_of::<MouseInput>() == if cfg!(target_pointer_width = "64") { 32 } else { 24 });
const _: () = assert!(std::mem::size_of::<KeybdInput>() == if cfg!(target_pointer_width = "64") { 24 } else { 16 });

// ── Pure builders ─────────────────────────────────────────────────────────

pub fn mouse(flags: u32, dx: i32, dy: i32, data: u32) -> Input {
    Input {
        type_: INPUT_MOUSE,
        u: InputUnion {
            mi: MouseInput { dx, dy, mouse_data: data, dw_flags: flags, time: 0, dw_extra_info: OWN_EXTRA_INFO },
        },
    }
}

fn keybd(vk: u16, scan: u16, flags: u32) -> Input {
    Input {
        type_: INPUT_KEYBOARD,
        u: InputUnion {
            ki: KeybdInput { w_vk: vk, w_scan: scan, dw_flags: flags, time: 0, dw_extra_info: OWN_EXTRA_INFO },
        },
    }
}

pub fn abs_move(ax: i64, ay: i64) -> Input {
    mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax as i32, ay as i32, 0)
}

pub fn button_flag(b: Button, down: bool) -> u32 {
    match (b, down) {
        (Button::Left, true) => MOUSEEVENTF_LEFTDOWN,
        (Button::Left, false) => MOUSEEVENTF_LEFTUP,
        (Button::Right, true) => MOUSEEVENTF_RIGHTDOWN,
        (Button::Right, false) => MOUSEEVENTF_RIGHTUP,
        (Button::Middle, true) => MOUSEEVENTF_MIDDLEDOWN,
        (Button::Middle, false) => MOUSEEVENTF_MIDDLEUP,
    }
}

/// Scan-code key event. `wVk` is 0: the position is the identity.
pub fn key_input(scan: Scan, down: bool) -> Input {
    let mut flags = KEYEVENTF_SCANCODE;
    if scan.extended {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    if !down {
        flags |= KEYEVENTF_KEYUP;
    }
    keybd(0, scan.code, flags)
}

/// Wire wheel steps → Windows wheel delta. The wire follows DOM convention
/// (positive `dy` scrolls **down**) and Windows is the opposite (positive is away
/// from the user, i.e. up), so vertical is negated; horizontal is the same sign
/// in both. `const` so the arithmetic is checked by the compiler.
///
/// ⚠ The sign is an assumption about the browser input engine (RC-015), which
/// does not exist yet. It must be confirmed against a real wheel event.
pub const fn wheel_delta(steps: f64) -> i32 {
    let s = if steps > 10.0 {
        10.0
    } else if steps < -10.0 {
        -10.0
    } else {
        steps
    };
    let scaled = s * (WHEEL_DELTA as f64);
    // Round half away from zero without `f64::round` (not `const`).
    (if scaled >= 0.0 { scaled + 0.5 } else { scaled - 0.5 }) as i32
}

/// Text as keyboard input. LF and TAB become the Enter and Tab keys; every other
/// scalar goes as UTF-16 `KEYEVENTF_UNICODE` down/up pairs (so an emoji, which is
/// a surrogate pair, becomes two pairs, as Windows expects).
pub fn text_inputs(text: &str) -> Vec<Input> {
    let mut out = Vec::with_capacity(text.len() * 2);
    for ch in text.chars() {
        match ch {
            '\n' => {
                let s = scan_for("Enter").expect("Enter is in the key map");
                out.push(key_input(s, true));
                out.push(key_input(s, false));
            }
            '\t' => {
                let s = scan_for("Tab").expect("Tab is in the key map");
                out.push(key_input(s, true));
                out.push(key_input(s, false));
            }
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push(keybd(0, *unit, KEYEVENTF_UNICODE));
                    out.push(keybd(0, *unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
                }
            }
        }
    }
    out
}

// ── The OS boundary, abstracted so the logic is testable ──────────────────

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum SendError {
    /// Access denied: a secure desktop, the lock screen, or a UAC prompt.
    Blocked,
    /// Fewer events were accepted than sent.
    Partial,
    Failed,
}

pub trait InputSink {
    /// Send all of `inputs` in **one** call, or fail.
    fn send(&mut self, inputs: &[Input]) -> Result<(), SendError>;
    /// The cursor's physical position, if it can be read.
    fn cursor(&self) -> Option<(i32, i32)>;
}

#[cfg(windows)]
pub use win::Win32Sink;

#[cfg(windows)]
mod win {
    use super::{Input, InputSink, SendError};

    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }

    #[link(name = "user32")]
    extern "system" {
        fn SendInput(c_inputs: u32, p_inputs: *const Input, cb_size: i32) -> u32;
        fn GetCursorPos(p: *mut Point) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetLastError() -> u32;
    }

    const ERROR_ACCESS_DENIED: u32 = 5;

    pub struct Win32Sink;

    impl InputSink for Win32Sink {
        fn send(&mut self, inputs: &[Input]) -> Result<(), SendError> {
            if inputs.is_empty() {
                return Ok(());
            }
            let n = inputs.len() as u32;
            // SAFETY: `inputs` is a valid slice of correctly sized INPUT structs.
            let sent = unsafe { SendInput(n, inputs.as_ptr(), std::mem::size_of::<Input>() as i32) };
            if sent == n {
                return Ok(());
            }
            if sent == 0 {
                // SAFETY: no preconditions.
                return Err(if unsafe { GetLastError() } == ERROR_ACCESS_DENIED {
                    SendError::Blocked
                } else {
                    SendError::Failed
                });
            }
            Err(SendError::Partial)
        }

        fn cursor(&self) -> Option<(i32, i32)> {
            let mut p = Point { x: 0, y: 0 };
            // SAFETY: `p` is a valid out-pointer.
            if unsafe { GetCursorPos(&mut p) } != 0 {
                Some((p.x, p.y))
            } else {
                None
            }
        }
    }
}

// ── The adapter ───────────────────────────────────────────────────────────

/// The acceptance budget: "Pointer error target ≤2 physical pixels".
pub const POINTER_ERROR_BUDGET_PX: i32 = 2;

/// Reports whether the owner is physically holding a modifier. Implemented by
/// [`crate::provenance::PhysicalState`]; a trait so tests need no hook.
pub trait HeldModifiers {
    fn modifier_held(&self, dom_code: &str) -> bool;
}

impl HeldModifiers for crate::provenance::PhysicalState {
    fn modifier_held(&self, dom_code: &str) -> bool {
        crate::provenance::PhysicalState::modifier_held(self, dom_code)
    }
}

impl<T: HeldModifiers> HeldModifiers for std::sync::Arc<T> {
    fn modifier_held(&self, dom_code: &str) -> bool {
        (**self).modifier_held(dom_code)
    }
}

pub struct WindowsInput<K: InputSink, H: HeldModifiers> {
    sink: K,
    held: H,
    display: DisplayDescriptor,
    virtual_desktop: Bounds,
    verify_pointer: bool,
}

impl<K: InputSink, H: HeldModifiers> WindowsInput<K, H> {
    /// `display` is the one display the owner chose; `virtual_desktop` is the
    /// bounding box of all displays *now*. A topology change must replace both
    /// (and the core ends the grant, so in practice by building a new adapter).
    pub fn new(sink: K, held: H, display: DisplayDescriptor, virtual_desktop: Bounds) -> Self {
        Self { sink, held, display, virtual_desktop, verify_pointer: true }
    }

    pub fn sink(&self) -> &K {
        &self.sink
    }

    /// For measuring with a real device where reading the cursor back is itself
    /// what is being tested. Leave on in production.
    pub fn set_verify_pointer(&mut self, on: bool) {
        self.verify_pointer = on;
    }

    /// The physical pixel and `SendInput` absolute coordinates for `(u, v)`.
    fn target(&self, u: f64, v: f64) -> ((i64, i64), (i64, i64)) {
        let (px, py) = desktop_point(u, v, self.display.bounds);
        (( px, py), absolute_point(px, py, self.virtual_desktop))
    }

    fn send(&mut self, inputs: &[Input]) -> Result<(), AdapterError> {
        self.sink.send(inputs).map_err(|e| {
            AdapterError(match e {
                SendError::Blocked => "input_blocked",
                SendError::Partial => "input_partial",
                SendError::Failed => "input_failed",
            })
        })
    }

    fn check_pointer(&self, want: (i64, i64)) -> Result<(), AdapterError> {
        if !self.verify_pointer {
            return Ok(());
        }
        // Read twice: the first read can precede the OS applying the move.
        for attempt in 0..2 {
            match self.sink.cursor() {
                None => return Ok(()), // cannot verify; do not invent a failure
                Some((x, y)) => {
                    let ex = (x as i64 - want.0).abs();
                    let ey = (y as i64 - want.1).abs();
                    if ex <= POINTER_ERROR_BUDGET_PX as i64 && ey <= POINTER_ERROR_BUDGET_PX as i64 {
                        return Ok(());
                    }
                }
            }
            if attempt == 0 {
                std::thread::yield_now();
            }
        }
        Err(AdapterError("pointer_error"))
    }
}

impl<K: InputSink, H: HeldModifiers> InputAdapter for WindowsInput<K, H> {
    fn supports_key(&self, code: &str) -> bool {
        scan_for(code).is_some()
    }

    fn inject(&mut self, event: &ValidatedEvent) -> Result<(), AdapterError> {
        match event.action() {
            Action::MoveTo { u, v } => {
                let (px, abs) = self.target(*u, *v);
                self.send(&[abs_move(abs.0, abs.1)])?;
                self.check_pointer(px)
            }
            Action::Button { button, down, u, v } => {
                let (px, abs) = self.target(*u, *v);
                // Position and button in ONE call: nothing can slip in between.
                self.send(&[abs_move(abs.0, abs.1), mouse(button_flag(*button, *down), 0, 0, 0)])?;
                self.check_pointer(px)
            }
            Action::Wheel { dx, dy, u, v } => {
                let (px, abs) = self.target(*u, *v);
                let mut batch = vec![abs_move(abs.0, abs.1)];
                // DOM: positive dy scrolls down. Windows: positive is up.
                let vert = -wheel_delta(*dy);
                let horiz = wheel_delta(*dx);
                if vert != 0 {
                    batch.push(mouse(MOUSEEVENTF_WHEEL, 0, 0, vert as u32));
                }
                if horiz != 0 {
                    batch.push(mouse(MOUSEEVENTF_HWHEEL, 0, 0, horiz as u32));
                }
                self.send(&batch)?;
                self.check_pointer(px)
            }
            Action::Key { code, down } => {
                let scan = scan_for(code).ok_or(AdapterError("unsupported_key"))?;
                self.send(&[key_input(scan, *down)])
            }
            Action::Text { text } => {
                let inputs = text_inputs(text);
                self.send(&inputs)
            }
        }
    }

    fn release(&mut self, what: &Release) -> Result<(), AdapterError> {
        match what {
            Release::Key(code) => {
                let scan = scan_for(code).ok_or(AdapterError("unsupported_key"))?;
                self.send(&[key_input(scan, false)])
            }
            // A button-up needs no position: it releases wherever the cursor is.
            Release::Button(b) => self.send(&[mouse(button_flag(*b, false), 0, 0, 0)]),
        }
    }

    fn physically_held(&self, code: &str) -> bool {
        self.held.modifier_held(code)
    }
}

// ── compile-time arithmetic checks ────────────────────────────────────────

const _: () = assert!(wheel_delta(1.0) == 120, "one logical step is one notch");
const _: () = assert!(wheel_delta(-1.0) == -120);
const _: () = assert!(wheel_delta(0.0) == 0);
const _: () = assert!(wheel_delta(0.5) == 60);
const _: () = assert!(wheel_delta(10.0) == 1200, "the contract's maximum");
const _: () = assert!(wheel_delta(99.0) == 1200, "clamped, never overflowing");
const _: () = assert!(wheel_delta(-99.0) == -1200);

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    #[derive(Default)]
    struct Shared {
        calls: Vec<Vec<(u32, u32, u32, i32, i32, u32, u16, u16)>>, // type, flags, data, dx, dy, extra-ok, vk, scan
        fail_with: Option<SendError>,
        cursor: Option<(i32, i32)>,
    }

    #[derive(Clone, Default)]
    struct FakeSink(Rc<RefCell<Shared>>);

    fn flatten(i: &Input) -> (u32, u32, u32, i32, i32, u32, u16, u16) {
        // SAFETY: tests only read the union member matching `type_`.
        unsafe {
            if i.type_ == INPUT_MOUSE {
                let m = i.u.mi;
                (m.dw_flags, 0, m.mouse_data, m.dx, m.dy, (m.dw_extra_info == OWN_EXTRA_INFO) as u32, 0, 0)
            } else {
                let k = i.u.ki;
                (k.dw_flags, 1, 0, 0, 0, (k.dw_extra_info == OWN_EXTRA_INFO) as u32, k.w_vk, k.w_scan)
            }
        }
    }

    impl InputSink for FakeSink {
        fn send(&mut self, inputs: &[Input]) -> Result<(), SendError> {
            let mut s = self.0.borrow_mut();
            if let Some(e) = s.fail_with {
                return Err(e);
            }
            s.calls.push(inputs.iter().map(flatten).collect());
            Ok(())
        }
        fn cursor(&self) -> Option<(i32, i32)> {
            self.0.borrow().cursor
        }
    }

    struct Held(Vec<&'static str>);
    impl HeldModifiers for Held {
        fn modifier_held(&self, c: &str) -> bool {
            self.0.contains(&c)
        }
    }

    fn desc(x: i32, y: i32, w: i32, h: i32) -> DisplayDescriptor {
        DisplayDescriptor {
            display_id: "d".into(),
            pixel_width: w as u32,
            pixel_height: h as u32,
            rotation: 0,
            bounds: Bounds { x, y, width: w, height: h },
            dpi_scale: 1.0,
            primary: x == 0,
        }
    }

    fn adapter(sink: FakeSink) -> WindowsInput<FakeSink, Held> {
        let d = desc(0, 0, 1920, 1080);
        WindowsInput::new(sink, Held(vec![]), d.clone(), d.bounds)
    }

    fn ev(a: Action) -> ValidatedEvent {
        // `ValidatedEvent::new` is crate-private in core by design. This unit-test
        // shortcut exists only behind core's `test-support` feature (a
        // dev-dependency). The production path - through the real gate - is
        // exercised in `tests/through_the_gate.rs`.
        shiksha_companion_core::adapter::test_support::validated(a)
    }

    #[test]
    fn input_is_the_size_windows_demands() {
        assert_eq!(std::mem::size_of::<Input>(), if cfg!(target_pointer_width = "64") { 40 } else { 28 });
    }

    #[test]
    fn a_click_sends_the_move_and_the_button_in_one_call_with_our_tag() {
        let sink = FakeSink::default();
        sink.0.borrow_mut().cursor = Some((960, 540));
        let mut a = adapter(sink.clone());
        a.inject(&ev(Action::Button { button: Button::Left, down: true, u: 0.5, v: 0.5 })).unwrap();
        let calls = &sink.0.borrow().calls;
        assert_eq!(calls.len(), 1, "one SendInput call");
        assert_eq!(calls[0].len(), 2);
        let (mv, click) = (calls[0][0], calls[0][1]);
        assert_eq!(mv.0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK);
        assert_eq!(click.0, MOUSEEVENTF_LEFTDOWN);
        assert_eq!((mv.5, click.5), (1, 1), "both tagged as ours");
    }

    #[test]
    fn the_absolute_coordinates_land_on_the_pixel_centre() {
        let sink = FakeSink::default();
        sink.0.borrow_mut().cursor = Some((0, 0));
        let mut a = adapter(sink.clone());
        a.inject(&ev(Action::MoveTo { u: 0.0, v: 0.0 })).unwrap();
        let mv = sink.0.borrow().calls[0][0];
        // pixel 0 of 1920: floor((2*0+1)*32768/1920) = 17
        assert_eq!((mv.3, mv.4), (17, 30));
    }

    #[test]
    fn a_display_left_of_primary_maps_into_the_left_half_of_the_virtual_desktop() {
        let sink = FakeSink::default();
        let left = desc(-1920, 0, 1920, 1080);
        let virt = Bounds { x: -1920, y: 0, width: 3840, height: 1080 };
        let mut a = WindowsInput::new(sink.clone(), Held(vec![]), left, virt);
        a.set_verify_pointer(false);
        a.inject(&ev(Action::MoveTo { u: 0.0, v: 0.0 })).unwrap();
        a.inject(&ev(Action::MoveTo { u: 1.0, v: 0.0 })).unwrap();
        let calls = &sink.0.borrow().calls;
        let (first, second) = (calls[0][0].3, calls[1][0].3);
        assert!(first < 100, "far left, not the middle: {first}");
        assert!((32_000..33_000).contains(&second), "just left of the middle: {second}");
    }

    #[test]
    fn a_key_goes_as_a_scan_code_with_no_virtual_key_and_the_extended_flag_where_needed() {
        let sink = FakeSink::default();
        let mut a = adapter(sink.clone());
        a.inject(&ev(Action::Key { code: "KeyA".into(), down: true })).unwrap();
        a.inject(&ev(Action::Key { code: "ArrowUp".into(), down: false })).unwrap();
        let calls = &sink.0.borrow().calls;
        let a_down = calls[0][0];
        assert_eq!((a_down.6, a_down.7), (0, 0x1E), "wVk is 0; the position is the identity");
        assert_eq!(a_down.0, KEYEVENTF_SCANCODE);
        let up = calls[1][0];
        assert_eq!(up.7, 0x48);
        assert_eq!(up.0, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP);
    }

    #[test]
    fn text_is_unicode_units_with_lf_and_tab_as_keys_and_emoji_as_a_surrogate_pair() {
        let inputs = text_inputs("a\n\t\u{1F600}");
        let f: Vec<_> = inputs.iter().map(flatten).collect();
        // 'a': down+up unicode
        assert_eq!((f[0].0, f[0].7), (KEYEVENTF_UNICODE, b'a' as u16));
        assert_eq!(f[1].0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP);
        // LF -> Enter key (scan 0x1C, not extended)
        assert_eq!((f[2].0, f[2].7), (KEYEVENTF_SCANCODE, 0x1C));
        // TAB -> Tab key (0x0F)
        assert_eq!(f[4].7, 0x0F);
        // U+1F600 -> D83D DE00, each a down/up pair
        assert_eq!(f[6].7, 0xD83D);
        assert_eq!(f[8].7, 0xDE00);
        assert_eq!(f.len(), 10);
        assert!(f.iter().all(|x| x.5 == 1), "all tagged");
    }

    #[test]
    fn the_wheel_negates_vertical_for_dom_convention_and_skips_zero_axes() {
        let sink = FakeSink::default();
        sink.0.borrow_mut().cursor = Some((960, 540));
        let mut a = adapter(sink.clone());
        a.inject(&ev(Action::Wheel { dx: 0.0, dy: 1.0, u: 0.5, v: 0.5 })).unwrap();
        a.inject(&ev(Action::Wheel { dx: 2.0, dy: 0.0, u: 0.5, v: 0.5 })).unwrap();
        a.inject(&ev(Action::Wheel { dx: 0.0, dy: 0.0, u: 0.5, v: 0.5 })).unwrap();
        let calls = &sink.0.borrow().calls;
        assert_eq!(calls[0].len(), 2, "move + vertical only");
        assert_eq!(calls[0][1].0, MOUSEEVENTF_WHEEL);
        assert_eq!(calls[0][1].2 as i32, -120, "DOM down is Windows negative");
        assert_eq!(calls[1].len(), 2, "move + horizontal only");
        assert_eq!(calls[1][1].0, MOUSEEVENTF_HWHEEL);
        assert_eq!(calls[1][1].2 as i32, 240);
        assert_eq!(calls[2].len(), 1, "a zero wheel is just a move");
    }

    #[test]
    fn a_pointer_that_did_not_land_is_a_reported_failure_not_silent_drift() {
        let sink = FakeSink::default();
        sink.0.borrow_mut().cursor = Some((960, 540));
        let mut a = adapter(sink.clone());
        // Target pixel for (0.5,0.5) is (960, 540): within budget.
        assert!(a.inject(&ev(Action::MoveTo { u: 0.5, v: 0.5 })).is_ok());
        // The cursor is somewhere else entirely (e.g. the OS refused the move).
        sink.0.borrow_mut().cursor = Some((10, 10));
        assert_eq!(a.inject(&ev(Action::MoveTo { u: 0.5, v: 0.5 })), Err(AdapterError("pointer_error")));
        // 2 px off is inside the budget; 3 is not.
        sink.0.borrow_mut().cursor = Some((962, 542));
        assert!(a.inject(&ev(Action::MoveTo { u: 0.5, v: 0.5 })).is_ok());
        sink.0.borrow_mut().cursor = Some((963, 540));
        assert!(a.inject(&ev(Action::MoveTo { u: 0.5, v: 0.5 })).is_err());
    }

    #[test]
    fn an_unreadable_cursor_does_not_invent_a_failure() {
        let sink = FakeSink::default(); // cursor() == None
        let mut a = adapter(sink);
        assert!(a.inject(&ev(Action::MoveTo { u: 0.5, v: 0.5 })).is_ok());
    }

    #[test]
    fn os_refusals_map_to_clear_codes_the_core_ends_the_grant_on() {
        for (e, code) in [
            (SendError::Blocked, "input_blocked"),
            (SendError::Partial, "input_partial"),
            (SendError::Failed, "input_failed"),
        ] {
            let sink = FakeSink::default();
            sink.0.borrow_mut().fail_with = Some(e);
            let mut a = adapter(sink);
            assert_eq!(
                a.inject(&ev(Action::Key { code: "KeyA".into(), down: true })),
                Err(AdapterError(code))
            );
        }
    }

    #[test]
    fn releasing_a_key_sends_only_the_key_up_and_a_button_up_needs_no_position() {
        let sink = FakeSink::default();
        let mut a = adapter(sink.clone());
        a.release(&Release::Key("ShiftLeft".into())).unwrap();
        a.release(&Release::Button(Button::Right)).unwrap();
        let calls = &sink.0.borrow().calls;
        assert_eq!(calls[0].len(), 1);
        assert_eq!(calls[0][0].0, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP);
        assert_eq!(calls[1].len(), 1, "no move");
        assert_eq!(calls[1][0].0, MOUSEEVENTF_RIGHTUP);
    }

    #[test]
    fn an_unknown_key_is_refused_before_anything_is_sent() {
        let sink = FakeSink::default();
        let mut a = adapter(sink.clone());
        assert!(!a.supports_key("PrintScreen"));
        assert_eq!(
            a.inject(&ev(Action::Key { code: "PrintScreen".into(), down: true })),
            Err(AdapterError("unsupported_key"))
        );
        assert!(sink.0.borrow().calls.is_empty());
    }

    #[test]
    fn every_allowlisted_key_is_supported() {
        let a = adapter(FakeSink::default());
        for k in shiksha_companion_core::wire::KEY_ALLOWLIST {
            assert!(a.supports_key(k), "{k}");
        }
    }

    #[test]
    fn physical_modifier_state_comes_from_the_monitor_not_from_async_key_state() {
        let d = desc(0, 0, 1920, 1080);
        let a = WindowsInput::new(FakeSink::default(), Held(vec!["ShiftLeft"]), d.clone(), d.bounds);
        assert!(a.physically_held("ShiftLeft"));
        assert!(!a.physically_held("ShiftRight"));
    }
}
