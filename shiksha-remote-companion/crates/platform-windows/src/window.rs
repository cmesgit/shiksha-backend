//! Native windows: the consent dialog and the always-visible status window.
//!
//! **Thin on purpose.** Every rule about consent — deny by default, the dwell, the
//! need for a physical hand, deny-on-timeout — lives in
//! [`shiksha_companion_core::consent::ConsentDialog`], which is tested. This module
//! only draws its [`ConsentView`] with ordinary Win32 controls and forwards what
//! the owner does. If a rule seems to belong here, it belongs in core.
//!
//! ## Why plain Win32 controls
//! `STATIC` and `BUTTON` controls are exposed to screen readers (UI Automation)
//! with no extra work, give native keyboard behaviour (Tab, Space, Enter, Escape
//! via `IsDialogMessageW`), honour high-contrast themes and system fonts, and
//! cannot be restyled into something misleading. A disabled button is not
//! tabbable, which is exactly the dialog model: Allow is unreachable until the
//! dwell ends. **Deny is the default push button**, so Enter denies.
//!
//! ## How provenance reaches the dialog (T13)
//! A click or key arrives as a window message, which carries no "was this
//! injected?" mark. The low-level hook ([`crate::provenance`]) *does* see that mark,
//! and runs before the message is delivered. Before every event the window drains
//! [`PhysicalState::take_activity`] into a [`PhysicalCorrelator`], and an Allow is
//! judged with [`PhysicalCorrelator::consume`]: physical only if a non-injected
//! event was seen in the last 500 ms, and that observation is *spent* by the
//! attempt. The caller must keep a [`crate::provenance::PhysicalMonitor`] running
//! while a consent window is open; without it nothing is ever physical and Allow
//! can never succeed (the safe failure).
//!
//! 🔴 Residual weakness, stated plainly: an injected click placed within 500 ms
//! *after* a real input event, and before any other, would be taken as physical.
//! The one-shot token stops a *burst*, not a single well-timed event. Closing this
//! fully needs the click's own provenance (a per-window raw-input check), which is
//! not done.
//!
//! ## Not done / not verified
//! * **Nothing here has ever drawn a window.** It type-checks; the first real run
//!   will find layout, font, focus and z-order problems.
//! * **Screen-reader and keyboard-only behaviour is untested.** Native controls make
//!   them *likely* to work; "native UI requires separate keyboard/screen-reader
//!   tests; web accessibility does not certify it" (frontend-ux-specification.md).
//! * `SetForegroundWindow` may be refused by the OS's foreground lock; the window is
//!   topmost regardless, but may not take keyboard focus until clicked.
//! * Fonts and layout use the system DPI at open time, with the default GUI font.
//!   Per-monitor DPI changes while open are not handled.
//! * x64 only (`GetWindowLongPtrW` is not an export on 32-bit Windows).

// ── Pure helpers (cross-platform, testable) ───────────────────────────────

pub const ID_OK: i32 = 1;
pub const ID_CANCEL: i32 = 2;
pub const ID_ALLOW: i32 = 1001;
pub const ID_DENY: i32 = 1002;
pub const ID_STOP: i32 = 2001;

pub const BN_CLICKED: u32 = 0;

/// What a `WM_COMMAND` means to the dialog.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Command {
    Allow,
    Deny,
    /// `IDCANCEL`: what the dialog manager sends for Escape.
    Escape,
}

pub fn command_id(w_param: usize) -> i32 {
    (w_param & 0xFFFF) as i32
}

pub fn notify_code(w_param: usize) -> u32 {
    ((w_param >> 16) & 0xFFFF) as u32
}

/// Only a *click* (or its keyboard equivalent) counts. Focus, hover and other
/// notifications from the same control must never be read as a press.
pub fn map_command(id: i32, notify: u32) -> Option<Command> {
    if id == ID_CANCEL {
        // Escape arrives with a notify code of 0 from the dialog manager.
        return if notify == 0 { Some(Command::Escape) } else { None };
    }
    if notify != BN_CLICKED {
        return None;
    }
    match id {
        ID_ALLOW => Some(Command::Allow),
        ID_DENY => Some(Command::Deny),
        _ => None,
    }
}

/// NUL-terminated UTF-16 for Win32 `W` functions.
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// CRLF-joined text for a multi-line static control.
pub fn join_lines(lines: &[String]) -> String {
    lines.join("\r\n\r\n")
}

/// Scale a 96-dpi pixel value to `dpi`, rounding to nearest.
pub fn scale(px: i32, dpi: u32) -> i32 {
    ((px as i64 * dpi as i64 + 48) / 96) as i32
}

/// The permanent "you are being controlled" text.
pub fn status_text(controller_name: &str, shortcut: &str) -> String {
    format!("{controller_name} is controlling this computer.\r\nStop at any time: {shortcut}")
}

// 96-dpi layout, in pixels.
pub const CLIENT_WIDTH: i32 = 520;
pub const MARGIN: i32 = 20;
pub const GAP: i32 = 16;
pub const BUTTON_W: i32 = 150;
pub const BUTTON_H: i32 = 40;
pub const STATUS_WIDTH: i32 = 340;
pub const STATUS_HEIGHT: i32 = 120;

// ── Win32 (x64 only) ──────────────────────────────────────────────────────

#[cfg(all(windows, target_pointer_width = "64"))]
pub use native::{run_consent, ConsentResult, StatusWindow, WindowError};

#[cfg(all(windows, target_pointer_width = "64"))]
mod native {
    use super::*;
    use std::ffi::c_void;
    use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
    use std::sync::{Arc, Mutex};

    use shiksha_companion_core::clock::MonotonicClock;
    use shiksha_companion_core::companion::StopLatch;
    use shiksha_companion_core::consent::{
        ConsentDialog, ConsentView, Control, Decision, KeyPress, Outcome, PhysicalCorrelator,
    };

    use crate::provenance::PhysicalState;

    // ── structures ──

    #[repr(C)]
    #[derive(Copy, Clone, Default)]
    struct Rect {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }

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

    type WndProc = unsafe extern "system" fn(isize, u32, usize, isize) -> isize;

    #[repr(C)]
    struct WndClassExW {
        cb_size: u32,
        style: u32,
        lpfn_wnd_proc: WndProc,
        cb_cls_extra: i32,
        cb_wnd_extra: i32,
        h_instance: isize,
        h_icon: isize,
        h_cursor: isize,
        hbr_background: isize,
        lpsz_menu_name: *const u16,
        lpsz_class_name: *const u16,
        h_icon_sm: isize,
    }

    // Sizes Windows checks; a wrong one fails as an opaque error.
    const _: () = assert!(std::mem::size_of::<Rect>() == 16);
    const _: () = assert!(std::mem::size_of::<Msg>() == 48);
    const _: () = assert!(std::mem::size_of::<WndClassExW>() == 80);

    // ── functions ──

    #[link(name = "user32")]
    extern "system" {
        fn RegisterClassExW(c: *const WndClassExW) -> u16;
        #[allow(clippy::too_many_arguments)]
        fn CreateWindowExW(
            ex_style: u32,
            class: *const u16,
            title: *const u16,
            style: u32,
            x: i32,
            y: i32,
            w: i32,
            h: i32,
            parent: isize,
            menu: isize,
            instance: isize,
            param: *mut c_void,
        ) -> isize;
        fn DefWindowProcW(h: isize, msg: u32, w: usize, l: isize) -> isize;
        fn DestroyWindow(h: isize) -> i32;
        fn ShowWindow(h: isize, cmd: i32) -> i32;
        fn GetMessageW(msg: *mut Msg, h: isize, min: u32, max: u32) -> i32;
        fn TranslateMessage(msg: *const Msg) -> i32;
        fn DispatchMessageW(msg: *const Msg) -> isize;
        fn IsDialogMessageW(dlg: isize, msg: *mut Msg) -> i32;
        fn PostQuitMessage(code: i32);
        fn PostMessageW(h: isize, msg: u32, w: usize, l: isize) -> i32;
        fn SendMessageW(h: isize, msg: u32, w: usize, l: isize) -> isize;
        fn SetWindowTextW(h: isize, text: *const u16) -> i32;
        fn EnableWindow(h: isize, enable: i32) -> i32;
        fn SetFocus(h: isize) -> isize;
        fn SetForegroundWindow(h: isize) -> i32;
        fn SetTimer(h: isize, id: usize, ms: u32, proc_: isize) -> usize;
        fn KillTimer(h: isize, id: usize) -> i32;
        fn LoadCursorW(instance: isize, name: *const u16) -> isize;
        fn GetSystemMetrics(index: i32) -> i32;
        fn GetDpiForSystem() -> u32;
        fn AdjustWindowRectEx(r: *mut Rect, style: u32, menu: i32, ex_style: u32) -> i32;
        fn SetWindowLongPtrW(h: isize, index: i32, value: isize) -> isize;
        fn GetWindowLongPtrW(h: isize, index: i32) -> isize;
        fn GetDC(h: isize) -> isize;
        fn ReleaseDC(h: isize, dc: isize) -> i32;
        fn DrawTextW(dc: isize, text: *const u16, count: i32, r: *mut Rect, format: u32) -> i32;
    }

    #[link(name = "gdi32")]
    extern "system" {
        fn GetStockObject(index: i32) -> isize;
        fn SelectObject(dc: isize, obj: isize) -> isize;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> isize;
        fn GetLastError() -> u32;
    }

    // ── constants ──

    const WM_NCCREATE: u32 = 0x0081;
    const WM_DESTROY: u32 = 0x0002;
    const WM_CLOSE: u32 = 0x0010;
    const WM_COMMAND: u32 = 0x0111;
    const WM_TIMER: u32 = 0x0113;
    const WM_SETFONT: u32 = 0x0030;
    const WM_APP: u32 = 0x8000;
    const WM_APP_TEXT: u32 = WM_APP + 1;
    const WM_APP_CLOSE: u32 = WM_APP + 2;

    const WS_POPUP: u32 = 0x8000_0000;
    const WS_CAPTION: u32 = 0x00C0_0000;
    const WS_SYSMENU: u32 = 0x0008_0000;
    const WS_CHILD: u32 = 0x4000_0000;
    const WS_VISIBLE: u32 = 0x1000_0000;
    const WS_TABSTOP: u32 = 0x0001_0000;
    const WS_DISABLED: u32 = 0x0800_0000;
    const WS_BORDER: u32 = 0x0080_0000;
    const WS_EX_DLGMODALFRAME: u32 = 0x0000_0001;
    const WS_EX_TOPMOST: u32 = 0x0000_0008;
    const WS_EX_TOOLWINDOW: u32 = 0x0000_0080;
    const WS_EX_NOACTIVATE: u32 = 0x0800_0000;
    const BS_PUSHBUTTON: u32 = 0x0;
    const BS_DEFPUSHBUTTON: u32 = 0x1;
    const SS_NOPREFIX: u32 = 0x80;

    const SW_SHOW: i32 = 5;
    const SM_CXSCREEN: i32 = 0;
    const SM_CYSCREEN: i32 = 1;
    const GWLP_USERDATA: i32 = -21;
    const COLOR_WINDOW_BRUSH: isize = 5 + 1;
    const IDC_ARROW: usize = 32512;
    const DEFAULT_GUI_FONT: i32 = 17;
    const DT_WORDBREAK: u32 = 0x0010;
    const DT_CALCRECT: u32 = 0x0400;
    const DT_NOPREFIX: u32 = 0x0800;
    const ERROR_CLASS_ALREADY_EXISTS: u32 = 1410;
    const TICK_TIMER: usize = 1;
    const TICK_MS: u32 = 100;

    #[derive(Debug, PartialEq, Eq)]
    pub enum WindowError {
        RegisterClass,
        CreateWindow,
        ThreadFailed,
    }

    fn register(name: &str, proc_: WndProc, background: isize) -> Result<(), WindowError> {
        let class = wide(name);
        // SAFETY: plain Win32 calls with valid, NUL-terminated strings.
        unsafe {
            let inst = GetModuleHandleW(std::ptr::null());
            let wc = WndClassExW {
                cb_size: std::mem::size_of::<WndClassExW>() as u32,
                style: 0,
                lpfn_wnd_proc: proc_,
                cb_cls_extra: 0,
                cb_wnd_extra: 0,
                h_instance: inst,
                h_icon: 0,
                h_cursor: LoadCursorW(0, IDC_ARROW as *const u16),
                hbr_background: background,
                lpsz_menu_name: std::ptr::null(),
                lpsz_class_name: class.as_ptr(),
                h_icon_sm: 0,
            };
            if RegisterClassExW(&wc) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS {
                return Err(WindowError::RegisterClass);
            }
        }
        Ok(())
    }

    /// Height in pixels that `text` needs at `width`, in the default GUI font.
    fn text_height(parent: isize, font: isize, text: &str, width: i32) -> i32 {
        let t = wide(text);
        // SAFETY: GetDC/ReleaseDC are paired; the rect and string are valid.
        unsafe {
            let dc = GetDC(parent);
            let old = SelectObject(dc, font);
            let mut r = Rect { left: 0, top: 0, right: width, bottom: 0 };
            DrawTextW(dc, t.as_ptr(), -1, &mut r, DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
            SelectObject(dc, old);
            ReleaseDC(parent, dc);
            r.bottom - r.top
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn child(
        parent: isize,
        class: &str,
        text: &str,
        style: u32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        id: i32,
        font: isize,
    ) -> isize {
        let (c, t) = (wide(class), wide(text));
        // SAFETY: valid strings; `id` is passed as the HMENU, as child controls require.
        unsafe {
            let inst = GetModuleHandleW(std::ptr::null());
            let hwnd = CreateWindowExW(0, c.as_ptr(), t.as_ptr(), WS_CHILD | WS_VISIBLE | style, x, y, w, h, parent, id as isize, inst, std::ptr::null_mut());
            if hwnd != 0 {
                SendMessageW(hwnd, WM_SETFONT, font as usize, 1);
            }
            hwnd
        }
    }

    // ── consent window ────────────────────────────────────────────────────

    struct ConsentState {
        dialog: ConsentDialog,
        correlator: PhysicalCorrelator,
        physical: Arc<PhysicalState>,
        clock: Box<dyn MonotonicClock>,
        allow: isize,
    }

    impl ConsentState {
        /// Fold what the hook saw into the correlator. Called before **every**
        /// event, including timer ticks, or an observation taken by one path would
        /// be invisible to the other.
        fn sample(&mut self) -> shiksha_companion_core::clock::Mono {
            let now = self.clock.now();
            let seen = self.physical.take_activity();
            self.correlator.observe(now, seen);
            now
        }

        fn on_command(&mut self, cmd: Command) {
            let now = self.sample();
            match cmd {
                // Approval spends the physical token; nothing else does.
                Command::Allow => {
                    let origin = self.correlator.consume(now);
                    self.dialog.on_click(now, Control::Allow, origin);
                }
                Command::Deny => self.dialog.on_click(now, Control::Deny, self.correlator.origin(now)),
                Command::Escape => self.dialog.on_key(now, KeyPress::Escape, self.correlator.origin(now)),
            }
        }

        fn on_close(&mut self) {
            let now = self.sample();
            self.dialog.on_click(now, Control::Close, self.correlator.origin(now));
        }

        fn on_tick(&mut self) {
            let now = self.sample();
            self.dialog.tick(now);
        }

        fn view(&self) -> ConsentView {
            self.dialog.view(self.clock.now())
        }

        fn finished(&self) -> bool {
            self.dialog.outcome() != Outcome::Pending
        }

        fn paint(&self) {
            let v = self.view();
            let label = wide(&v.allow_label);
            // SAFETY: `allow` is the live Allow button.
            unsafe {
                SetWindowTextW(self.allow, label.as_ptr());
                EnableWindow(self.allow, v.allow_enabled as i32);
            }
        }
    }

    unsafe extern "system" fn consent_proc(h: isize, msg: u32, w: usize, l: isize) -> isize {
        if msg == WM_NCCREATE {
            // `lpCreateParams` is the first field of CREATESTRUCTW.
            let p = *(l as *const *mut c_void);
            SetWindowLongPtrW(h, GWLP_USERDATA, p as isize);
            return DefWindowProcW(h, msg, w, l);
        }
        let ptr = GetWindowLongPtrW(h, GWLP_USERDATA) as *mut ConsentState;
        if ptr.is_null() {
            return DefWindowProcW(h, msg, w, l);
        }
        // The `&mut` is dropped before `DestroyWindow`, which re-enters this
        // procedure with WM_DESTROY.
        let done = {
            let st = &mut *ptr;
            match msg {
                WM_COMMAND => {
                    if let Some(cmd) = map_command(command_id(w), notify_code(w)) {
                        st.on_command(cmd);
                    }
                }
                WM_TIMER => st.on_tick(),
                WM_CLOSE => st.on_close(),
                WM_DESTROY => {
                    PostQuitMessage(0);
                    return 0;
                }
                _ => return DefWindowProcW(h, msg, w, l),
            }
            st.paint();
            st.finished()
        };
        if done {
            KillTimer(h, TICK_TIMER);
            DestroyWindow(h);
        }
        0
    }

    pub struct ConsentResult {
        pub outcome: Outcome,
        /// Present only for an `Allowed` control dialog.
        pub decision: Option<Decision>,
        /// Allow attempts refused for being injected or too early.
        pub refused_approvals: u32,
    }

    /// Show `dialog` and block until it is decided. Run on a thread that is allowed
    /// to own a window and pump messages.
    ///
    /// `dialog` must have been opened with `clock.now()` (the same clock), and a
    /// [`crate::provenance::PhysicalMonitor`] must be running for the duration or
    /// Allow cannot succeed.
    pub fn run_consent<C: MonotonicClock + 'static>(
        dialog: ConsentDialog,
        clock: C,
        physical: Arc<PhysicalState>,
    ) -> Result<ConsentResult, WindowError> {
        register("ShikshaRemoteConsent", consent_proc, COLOR_WINDOW_BRUSH)?;

        let view = dialog.view(clock.now());
        let state = Box::new(ConsentState {
            dialog,
            correlator: PhysicalCorrelator::new(),
            physical,
            clock: Box::new(clock),
            allow: 0,
        });
        let raw = Box::into_raw(state);

        // SAFETY: `raw` stays valid until reclaimed after the loop; the window
        // procedure only dereferences it while the window exists.
        unsafe {
            let dpi = GetDpiForSystem().max(96);
            let font = GetStockObject(DEFAULT_GUI_FONT);
            let style = WS_CAPTION | WS_SYSMENU;
            let ex = WS_EX_TOPMOST | WS_EX_DLGMODALFRAME;

            let body = join_lines(&view.lines);
            let text_w = scale(CLIENT_WIDTH - 2 * MARGIN, dpi);
            let probe = 0; // measuring against the desktop DC is enough for height
            let text_h = text_height(probe, font, &body, text_w) + scale(4, dpi);
            let client_h = scale(MARGIN, dpi) + text_h + scale(GAP, dpi) + scale(BUTTON_H, dpi) + scale(MARGIN, dpi);
            let client_w = scale(CLIENT_WIDTH, dpi);

            let mut r = Rect { left: 0, top: 0, right: client_w, bottom: client_h };
            AdjustWindowRectEx(&mut r, style, 0, ex);
            let (ow, oh) = (r.right - r.left, r.bottom - r.top);
            let x = (GetSystemMetrics(SM_CXSCREEN) - ow) / 2;
            let y = (GetSystemMetrics(SM_CYSCREEN) - oh) / 3;

            let class = wide("ShikshaRemoteConsent");
            let title = wide(&view.title);
            let inst = GetModuleHandleW(std::ptr::null());
            let h = CreateWindowExW(ex, class.as_ptr(), title.as_ptr(), style, x, y, ow, oh, 0, 0, inst, raw as *mut c_void);
            if h == 0 {
                drop(Box::from_raw(raw));
                return Err(WindowError::CreateWindow);
            }

            let margin = scale(MARGIN, dpi);
            child(h, "STATIC", &body, SS_NOPREFIX, margin, margin, text_w, text_h, 0, font);
            let by = margin + text_h + scale(GAP, dpi);
            let (bw, bh) = (scale(BUTTON_W, dpi), scale(BUTTON_H, dpi));
            // Created Deny first, so it is first in the tab order. It is the
            // default push button: Enter denies.
            let deny = child(h, "BUTTON", &view.deny_label, WS_TABSTOP | BS_DEFPUSHBUTTON, margin, by, bw, bh, ID_DENY, font);
            let allow = child(
                h,
                "BUTTON",
                &view.allow_label,
                WS_TABSTOP | WS_DISABLED | BS_PUSHBUTTON,
                client_w - margin - bw,
                by,
                bw,
                bh,
                ID_ALLOW,
                font,
            );
            (*raw).allow = allow;
            (*raw).paint();

            ShowWindow(h, SW_SHOW);
            SetForegroundWindow(h);
            SetFocus(deny); // initial focus is Deny, never Allow
            SetTimer(h, TICK_TIMER, TICK_MS, 0);

            let mut msg: Msg = std::mem::zeroed();
            while GetMessageW(&mut msg, 0, 0, 0) > 0 {
                if IsDialogMessageW(h, &mut msg) == 0 {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
            }

            let st = Box::from_raw(raw);
            Ok(ConsentResult {
                outcome: st.dialog.outcome(),
                decision: st.dialog.decision(),
                refused_approvals: st.dialog.refused_approvals(),
            })
        }
    }

    // ── status window ─────────────────────────────────────────────────────

    struct StatusState {
        latch: Arc<StopLatch>,
        stop_pressed: Arc<AtomicBool>,
        text: Arc<Mutex<String>>,
        label: isize,
        closing: bool,
    }

    unsafe extern "system" fn status_proc(h: isize, msg: u32, w: usize, l: isize) -> isize {
        if msg == WM_NCCREATE {
            let p = *(l as *const *mut c_void);
            SetWindowLongPtrW(h, GWLP_USERDATA, p as isize);
            return DefWindowProcW(h, msg, w, l);
        }
        let ptr = GetWindowLongPtrW(h, GWLP_USERDATA) as *mut StatusState;
        if ptr.is_null() {
            return DefWindowProcW(h, msg, w, l);
        }
        let destroy = {
            let st = &mut *ptr;
            match msg {
                WM_COMMAND => {
                    // Stopping is the safe direction, so any origin may do it, and it
                    // acts on the lock-free latch: no lock, no server, works offline.
                    if command_id(w) == ID_STOP && notify_code(w) == BN_CLICKED {
                        st.latch.stop();
                        st.stop_pressed.store(true, Ordering::SeqCst);
                    }
                    false
                }
                WM_APP_TEXT => {
                    if let Ok(t) = st.text.lock() {
                        let wt = wide(&t);
                        SetWindowTextW(st.label, wt.as_ptr());
                    }
                    false
                }
                WM_APP_CLOSE => {
                    st.closing = true;
                    true
                }
                // 🔴 The owner cannot dismiss the indicator while it is needed
                // (Alt+F4 included): a status window that can be closed is one that
                // can be hidden. Only `StatusWindow::close` ends it.
                WM_CLOSE => {
                    if st.closing {
                        true
                    } else {
                        return 0;
                    }
                }
                WM_DESTROY => {
                    PostQuitMessage(0);
                    return 0;
                }
                _ => return DefWindowProcW(h, msg, w, l),
            }
        };
        if destroy {
            DestroyWindow(h);
        }
        0
    }

    /// The always-visible "Controlled by …" window with a Stop button.
    ///
    /// It is a tool window (no taskbar entry), always on top and **not activating**,
    /// so it never steals keyboard focus from whatever the owner is working in;
    /// the physical hotkey is the keyboard route to Stop. It has no close button and
    /// ignores Alt+F4: only [`StatusWindow::close`] removes it.
    pub struct StatusWindow {
        hwnd: Arc<AtomicIsize>,
        text: Arc<Mutex<String>>,
        stop_pressed: Arc<AtomicBool>,
        join: Option<std::thread::JoinHandle<()>>,
    }

    impl StatusWindow {
        /// Open it. Pressing Stop closes `latch` lock-free, then raises a flag the
        /// app polls with [`Self::take_stop_pressed`] to run the companion's cleanup.
        pub fn open(latch: Arc<StopLatch>, initial_text: String) -> Result<Self, WindowError> {
            let hwnd = Arc::new(AtomicIsize::new(0));
            let text = Arc::new(Mutex::new(initial_text));
            let stop_pressed = Arc::new(AtomicBool::new(false));
            let (tx, rx) = std::sync::mpsc::channel::<Result<(), WindowError>>();

            let (hw, txt, sp) = (hwnd.clone(), text.clone(), stop_pressed.clone());
            let join = std::thread::Builder::new()
                .name("status-window".into())
                .spawn(move || {
                    if let Err(e) = register("ShikshaRemoteStatus", status_proc, COLOR_WINDOW_BRUSH) {
                        let _ = tx.send(Err(e));
                        return;
                    }
                    let initial = txt.lock().map(|t| t.clone()).unwrap_or_default();
                    let raw = Box::into_raw(Box::new(StatusState {
                        latch,
                        stop_pressed: sp,
                        text: txt,
                        label: 0,
                        closing: false,
                    }));
                    // SAFETY: the window, its state and its message loop all live and
                    // die on this thread; `raw` is reclaimed after the loop.
                    unsafe {
                        let dpi = GetDpiForSystem().max(96);
                        let font = GetStockObject(DEFAULT_GUI_FONT);
                        let (w, hgt) = (scale(STATUS_WIDTH, dpi), scale(STATUS_HEIGHT, dpi));
                        let x = GetSystemMetrics(SM_CXSCREEN) - w - scale(16, dpi);
                        let y = scale(16, dpi);

                        let class = wide("ShikshaRemoteStatus");
                        let title = wide("Remote control active");
                        let inst = GetModuleHandleW(std::ptr::null());
                        let h = CreateWindowExW(
                            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                            class.as_ptr(),
                            title.as_ptr(),
                            WS_POPUP | WS_BORDER,
                            x,
                            y,
                            w,
                            hgt,
                            0,
                            0,
                            inst,
                            raw as *mut c_void,
                        );
                        if h == 0 {
                            drop(Box::from_raw(raw));
                            let _ = tx.send(Err(WindowError::CreateWindow));
                            return;
                        }
                        let m = scale(MARGIN / 2, dpi);
                        (*raw).label = child(h, "STATIC", &initial, SS_NOPREFIX, m, m, w - 2 * m, scale(56, dpi), 0, font);
                        child(
                            h,
                            "BUTTON",
                            "Stop",
                            WS_TABSTOP | BS_DEFPUSHBUTTON,
                            m,
                            scale(64, dpi),
                            w - 2 * m,
                            scale(44, dpi),
                            ID_STOP,
                            font,
                        );
                        hw.store(h, Ordering::SeqCst);
                        ShowWindow(h, SW_SHOW);
                        let _ = tx.send(Ok(()));

                        let mut msg: Msg = std::mem::zeroed();
                        while GetMessageW(&mut msg, 0, 0, 0) > 0 {
                            TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                        hw.store(0, Ordering::SeqCst);
                        drop(Box::from_raw(raw));
                    }
                })
                .map_err(|_| WindowError::ThreadFailed)?;

            match rx.recv() {
                Ok(Ok(())) => Ok(Self { hwnd, text, stop_pressed, join: Some(join) }),
                Ok(Err(e)) => {
                    let _ = join.join();
                    Err(e)
                }
                Err(_) => Err(WindowError::ThreadFailed),
            }
        }

        /// Replace the displayed text (safe from any thread).
        pub fn set_text(&self, t: String) {
            if let Ok(mut g) = self.text.lock() {
                *g = t;
            }
            let h = self.hwnd.load(Ordering::SeqCst);
            if h != 0 {
                // SAFETY: posting to a window owned by the status thread.
                unsafe {
                    PostMessageW(h, WM_APP_TEXT, 0, 0);
                }
            }
        }

        /// Has the owner pressed the Stop button since the last call? The latch is
        /// already closed; this tells the app to run cleanup and tell the controller.
        pub fn take_stop_pressed(&self) -> bool {
            self.stop_pressed.swap(false, Ordering::SeqCst)
        }

        /// Remove the window. Call when the grant ends.
        pub fn close(&mut self) {
            let h = self.hwnd.load(Ordering::SeqCst);
            if h != 0 {
                // SAFETY: posting to a window owned by the status thread.
                unsafe {
                    PostMessageW(h, WM_APP_CLOSE, 0, 0);
                }
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
        }
    }

    impl Drop for StatusWindow {
        fn drop(&mut self) {
            self.close();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_click_notification_counts_as_a_press() {
        assert_eq!(map_command(ID_ALLOW, BN_CLICKED), Some(Command::Allow));
        assert_eq!(map_command(ID_DENY, BN_CLICKED), Some(Command::Deny));
        // BN_SETFOCUS (6), BN_KILLFOCUS (7), BN_HILITE (2) and the rest are not.
        for n in [1, 2, 3, 4, 5, 6, 7, 8] {
            assert_eq!(map_command(ID_ALLOW, n), None, "notify {n}");
            assert_eq!(map_command(ID_DENY, n), None, "notify {n}");
        }
    }

    #[test]
    fn escape_is_deny_and_nothing_else_maps_to_allow() {
        assert_eq!(map_command(ID_CANCEL, 0), Some(Command::Escape));
        assert_eq!(map_command(ID_OK, 0), None, "IDOK must never approve");
        assert_eq!(map_command(ID_STOP, BN_CLICKED), None, "the status Stop is not a dialog command");
        assert_eq!(map_command(0, 0), None);
        assert_eq!(map_command(-1, 0), None);
    }

    #[test]
    fn wparam_splits_into_control_id_and_notification() {
        let w = (BN_CLICKED as usize) << 16 | ID_ALLOW as usize;
        assert_eq!((command_id(w), notify_code(w)), (ID_ALLOW, BN_CLICKED));
        let w = (6usize << 16) | ID_DENY as usize;
        assert_eq!((command_id(w), notify_code(w)), (ID_DENY, 6));
    }

    #[test]
    fn wide_strings_are_nul_terminated_utf16() {
        assert_eq!(wide("a"), vec![0x61, 0]);
        assert_eq!(wide(""), vec![0]);
        assert_eq!(wide("\u{1F600}"), vec![0xD83D, 0xDE00, 0]);
        assert_eq!(wide("Asha Rao — Maths")[0], 'A' as u16);
    }

    #[test]
    fn scaling_rounds_to_nearest_and_is_the_identity_at_96_dpi() {
        assert_eq!(scale(520, 96), 520);
        assert_eq!(scale(100, 144), 150);
        assert_eq!(scale(100, 120), 125);
        assert_eq!(scale(0, 192), 0);
        assert_eq!(scale(1, 144), 2, "1.5 rounds up");
    }

    #[test]
    fn the_body_is_crlf_separated_for_a_native_static_control() {
        let t = join_lines(&["one".into(), "two".into()]);
        assert!(t.contains("\r\n") && !t.contains("\n\n\n"));
        assert_eq!(t, "one\r\n\r\ntwo");
    }

    #[test]
    fn the_status_text_names_the_person_and_the_way_to_stop() {
        let t = status_text("Asha Rao", "Ctrl+Alt+Shift+F12");
        assert!(t.contains("Asha Rao"));
        assert!(t.contains("Ctrl+Alt+Shift+F12"));
        assert!(t.contains("controlling this computer"));
    }

    #[test]
    fn the_layout_leaves_room_for_two_buttons_side_by_side() {
        assert!(2 * BUTTON_W + GAP + 2 * MARGIN <= CLIENT_WIDTH, "Deny and Allow must not overlap");
        assert!(BUTTON_H >= 40, "targets stay comfortably large");
    }

    #[test]
    fn control_ids_are_distinct_and_clear_of_the_dialog_manager_ones() {
        let ids = [ID_ALLOW, ID_DENY, ID_STOP];
        for (i, a) in ids.iter().enumerate() {
            assert!(*a > 100, "stay above IDOK/IDCANCEL and friends");
            for b in &ids[i + 1..] {
                assert_ne!(a, b);
            }
        }
        assert_eq!((ID_OK, ID_CANCEL), (1, 2), "the dialog manager's own ids");
    }
}
