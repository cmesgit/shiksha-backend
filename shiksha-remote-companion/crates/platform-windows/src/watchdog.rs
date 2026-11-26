//! Cleanup when the companion process is killed while a key or button is held.
//!
//! `Drop` cannot run when a process is terminated, and Windows does **not** release
//! keys or buttons that a dead process injected: a Shift sent down and never up
//! stays down. `companion-architecture.md` therefore requires "a minimal same-user
//! watchdog/helper with a pipe-death signal", and says that if reliable cleanup
//! cannot be shown, ship **no indefinite holds**.
//!
//! ## Design
//!
//! A tiny helper process (`crates/watchdog`, no network, no authority to open any
//! gate — it can only *release*) reads lines from a pipe whose only write end is
//! held by the companion:
//!
//! ```text
//! H K ShiftLeft   H B left      the companion is ABOUT to hold this
//! F K ShiftLeft   F B left      it was released
//! Q                             clean exit
//! ```
//!
//! When the companion dies the OS closes its handles, the helper reads end-of-file,
//! and releases whatever it was told is held. No signal, no polling, no timer: the
//! OS closing the pipe *is* the death notice, which is why a pipe and not a
//! heartbeat.
//!
//! Two rules make it safe rather than merely present:
//!
//! 1. **Tell the helper before pressing.** [`GuardedAdapter`] sends `H` and only
//!    then injects. A crash between the two leaves the helper releasing something
//!    that was never down, which is harmless; the reverse order leaves a stuck key
//!    nobody knows about.
//! 2. **No watchdog, no holds.** If the helper cannot be told (dead, pipe broken),
//!    a key-*down* or button-*down* is refused with `watchdog_lost`, which ends
//!    the grant. Releases still go through. This is "restrict to bounded gestures"
//!    taken to its fail-closed conclusion.
//!
//! ## What is NOT demonstrated — read before certifying anything
//!
//! * T09 (kill the companion while holding Shift or dragging, and see the release)
//!   has **not been run**. Nothing here has executed against a real OS. By the
//!   document's own rule, held-input release is **not certified**.
//! * Killing the **helper too** (T09 asks for each independently) leaves keys
//!   stuck; no user-mode design survives both dying. The companion notices the
//!   helper's death at the next hold and refuses it, which bounds the exposure to
//!   what was already held.
//! * The helper cannot tell whether the *owner* is physically holding the same
//!   key, so it releases regardless. Ending a session cleanly goes through the
//!   companion, which does check.

use std::collections::BTreeSet;

use shiksha_companion_core::adapter::{Action, AdapterError, InputAdapter, ValidatedEvent};
use shiksha_companion_core::wire::Button;

/// Re-exported so the helper binary, which depends only on this crate, can name it.
pub use shiksha_companion_core::adapter::Release;

use crate::keymap;

// ── Protocol (pure) ───────────────────────────────────────────────────────

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Msg {
    Hold(Release),
    Free(Release),
    Quit,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ParseError {
    Empty,
    Unknown,
    BadKey,
    BadButton,
}

fn button_name(b: Button) -> &'static str {
    match b {
        Button::Left => "left",
        Button::Middle => "middle",
        Button::Right => "right",
    }
}

fn parse_button(s: &str) -> Option<Button> {
    match s {
        "left" => Some(Button::Left),
        "middle" => Some(Button::Middle),
        "right" => Some(Button::Right),
        _ => None,
    }
}

/// One line, no trailing newline. Only allowlisted key names and three button
/// names can ever be encoded, so nothing a caller supplies can inject a second
/// command into the stream.
pub fn encode(m: &Msg) -> Option<String> {
    Some(match m {
        Msg::Quit => "Q".to_string(),
        Msg::Hold(Release::Key(k)) | Msg::Free(Release::Key(k)) => {
            keymap::lookup(k)?; // refuse anything not in the key map (also kills newlines)
            format!("{} K {k}", if matches!(m, Msg::Hold(_)) { "H" } else { "F" })
        }
        Msg::Hold(Release::Button(b)) | Msg::Free(Release::Button(b)) => {
            format!("{} B {}", if matches!(m, Msg::Hold(_)) { "H" } else { "F" }, button_name(*b))
        }
    })
}

pub fn parse_line(line: &str) -> Result<Msg, ParseError> {
    let line = line.trim_end_matches(['\r', '\n']);
    if line.is_empty() {
        return Err(ParseError::Empty);
    }
    let mut it = line.split(' ');
    let verb = it.next().unwrap_or("");
    if verb == "Q" {
        return if it.next().is_none() { Ok(Msg::Quit) } else { Err(ParseError::Unknown) };
    }
    let (kind, arg) = match (it.next(), it.next(), it.next()) {
        (Some(k), Some(a), None) => (k, a),
        _ => return Err(ParseError::Unknown),
    };
    let what = match kind {
        "K" => {
            keymap::lookup(arg).ok_or(ParseError::BadKey)?;
            Release::Key(arg.to_string())
        }
        "B" => Release::Button(parse_button(arg).ok_or(ParseError::BadButton)?),
        _ => return Err(ParseError::Unknown),
    };
    match verb {
        "H" => Ok(Msg::Hold(what)),
        "F" => Ok(Msg::Free(what)),
        _ => Err(ParseError::Unknown),
    }
}

/// What the helper believes is down.
#[derive(Default)]
pub struct HoldTracker {
    keys: BTreeSet<String>,
    buttons: BTreeSet<&'static str>,
}

impl HoldTracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn apply(&mut self, m: &Msg) {
        match m {
            Msg::Hold(Release::Key(k)) => {
                self.keys.insert(k.clone());
            }
            Msg::Free(Release::Key(k)) => {
                self.keys.remove(k);
            }
            Msg::Hold(Release::Button(b)) => {
                self.buttons.insert(button_name(*b));
            }
            Msg::Free(Release::Button(b)) => {
                self.buttons.remove(button_name(*b));
            }
            Msg::Quit => {}
        }
    }

    /// Everything to release: buttons first, so a drag ends before the modifiers
    /// that were held during it lift. Order among keys does not matter to the OS.
    pub fn release_all(&mut self) -> Vec<Release> {
        let mut out = Vec::new();
        for b in std::mem::take(&mut self.buttons) {
            if let Some(btn) = parse_button(b) {
                out.push(Release::Button(btn));
            }
        }
        for k in std::mem::take(&mut self.keys) {
            out.push(Release::Key(k));
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty() && self.buttons.is_empty()
    }
}

// ── The companion side ────────────────────────────────────────────────────

/// Where hold notices go. `Err` means the helper cannot be told.
pub trait HoldSink {
    fn notify(&mut self, m: &Msg) -> Result<(), ()>;
}

/// Wraps an adapter so every hold is announced to the helper **first**, and no
/// hold happens if it cannot be.
pub struct GuardedAdapter<A: InputAdapter, S: HoldSink> {
    inner: A,
    sink: S,
}

impl<A: InputAdapter, S: HoldSink> GuardedAdapter<A, S> {
    pub fn new(inner: A, sink: S) -> Self {
        Self { inner, sink }
    }
    pub fn inner(&self) -> &A {
        &self.inner
    }
    pub fn sink(&self) -> &S {
        &self.sink
    }
}

impl<A: InputAdapter, S: HoldSink> InputAdapter for GuardedAdapter<A, S> {
    fn supports_key(&self, code: &str) -> bool {
        self.inner.supports_key(code)
    }

    fn inject(&mut self, event: &ValidatedEvent) -> Result<(), AdapterError> {
        match event.action() {
            Action::Key { code, down: true } => {
                let what = Release::Key(code.clone());
                // 1. Announce. 2. Only then press. No watchdog => no hold.
                self.sink.notify(&Msg::Hold(what.clone())).map_err(|_| AdapterError("watchdog_lost"))?;
                let r = self.inner.inject(event);
                if r.is_err() {
                    // The press may not have happened; a spare key-up is harmless.
                    let _ = self.sink.notify(&Msg::Free(what));
                }
                r
            }
            Action::Button { button, down: true, .. } => {
                let what = Release::Button(*button);
                self.sink.notify(&Msg::Hold(what.clone())).map_err(|_| AdapterError("watchdog_lost"))?;
                let r = self.inner.inject(event);
                if r.is_err() {
                    let _ = self.sink.notify(&Msg::Free(what));
                }
                r
            }
            Action::Key { code, down: false } => {
                let r = self.inner.inject(event);
                // Free only after the release really went out. If the helper is
                // gone this is best-effort: an extra hold left in a dead helper
                // is harmless, and the release itself already happened.
                let _ = self.sink.notify(&Msg::Free(Release::Key(code.clone())));
                r
            }
            Action::Button { button, down: false, .. } => {
                let r = self.inner.inject(event);
                let _ = self.sink.notify(&Msg::Free(Release::Button(*button)));
                r
            }
            // Moves, wheel and text hold nothing.
            _ => self.inner.inject(event),
        }
    }

    fn release(&mut self, what: &Release) -> Result<(), AdapterError> {
        let r = self.inner.release(what);
        let _ = self.sink.notify(&Msg::Free(what.clone()));
        r
    }

    fn physically_held(&self, code: &str) -> bool {
        self.inner.physically_held(code)
    }
}

// ── Process plumbing ──────────────────────────────────────────────────────

/// The pipe to a spawned helper.
pub struct PipeHoldSink {
    child: std::process::Child,
    stdin: Option<std::process::ChildStdin>,
}

impl PipeHoldSink {
    /// Start the helper. Its stdin is the pipe: the companion holds the only write
    /// end (std marks it non-inheritable, so no other child can keep it open and
    /// mask our death). The helper gets no console window and no stdout/stderr.
    pub fn spawn(helper_path: &std::path::Path) -> std::io::Result<Self> {
        use std::process::{Command, Stdio};
        let mut cmd = Command::new(helper_path);
        cmd.stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take();
        Ok(Self { child, stdin })
    }

    /// Is the helper still running? Checked before each hold in addition to the
    /// write failing, because a write to a freshly dead pipe can still succeed
    /// once from a buffer.
    pub fn helper_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Clean shutdown: tell the helper to release anything left and exit.
    pub fn shutdown(&mut self) {
        let _ = self.notify(&Msg::Quit);
        self.stdin = None; // closing the pipe is the same signal as dying
        let _ = self.child.wait();
    }
}

impl HoldSink for PipeHoldSink {
    fn notify(&mut self, m: &Msg) -> Result<(), ()> {
        use std::io::Write;
        if !self.helper_alive() {
            return Err(());
        }
        let line = encode(m).ok_or(())?;
        let w = self.stdin.as_mut().ok_or(())?;
        w.write_all(line.as_bytes()).map_err(|_| ())?;
        w.write_all(b"\n").map_err(|_| ())?;
        w.flush().map_err(|_| ())
    }
}

impl Drop for PipeHoldSink {
    fn drop(&mut self) {
        // Dropping closes the pipe, which tells the helper to release and exit.
        self.stdin = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use shiksha_companion_core::adapter::test_support::validated;
    use shiksha_companion_core::adapter::FakeInputAdapter;

    // ── protocol ──

    #[test]
    fn every_message_round_trips() {
        for m in [
            Msg::Hold(Release::Key("ShiftLeft".into())),
            Msg::Free(Release::Key("ArrowUp".into())),
            Msg::Hold(Release::Button(Button::Left)),
            Msg::Free(Release::Button(Button::Middle)),
            Msg::Hold(Release::Button(Button::Right)),
            Msg::Quit,
        ] {
            let line = encode(&m).unwrap();
            assert_eq!(parse_line(&line), Ok(m.clone()), "{line}");
            assert_eq!(parse_line(&format!("{line}\r\n")), Ok(m), "tolerates CRLF");
        }
    }

    #[test]
    fn every_allowlisted_key_round_trips() {
        for k in shiksha_companion_core::wire::KEY_ALLOWLIST {
            let m = Msg::Hold(Release::Key(k.to_string()));
            assert_eq!(parse_line(&encode(&m).unwrap()), Ok(m), "{k}");
        }
    }

    #[test]
    fn nothing_outside_the_key_map_can_be_encoded_least_of_all_a_second_command() {
        assert_eq!(encode(&Msg::Hold(Release::Key("PrintScreen".into()))), None);
        assert_eq!(encode(&Msg::Hold(Release::Key("KeyA\nQ".into()))), None, "no smuggled newline");
        assert_eq!(encode(&Msg::Free(Release::Key("KeyA F K KeyB".into()))), None);
        assert_eq!(encode(&Msg::Hold(Release::Key(String::new()))), None);
    }

    #[test]
    fn malformed_lines_are_errors_not_guesses() {
        for (l, e) in [
            ("", ParseError::Empty),
            ("\n", ParseError::Empty),
            ("X K KeyA", ParseError::Unknown),
            ("H", ParseError::Unknown),
            ("H K", ParseError::Unknown),
            ("H K KeyA extra", ParseError::Unknown),
            ("H Z KeyA", ParseError::Unknown),
            ("H K PrintScreen", ParseError::BadKey),
            ("H B wheel", ParseError::BadButton),
            ("Q now", ParseError::Unknown),
            ("h k KeyA", ParseError::Unknown),
        ] {
            assert_eq!(parse_line(l), Err(e), "{l:?}");
        }
    }

    // ── tracker ──

    #[test]
    fn the_tracker_follows_holds_and_frees_and_releases_what_is_left() {
        let mut t = HoldTracker::new();
        t.apply(&Msg::Hold(Release::Key("ShiftLeft".into())));
        t.apply(&Msg::Hold(Release::Key("KeyA".into())));
        t.apply(&Msg::Hold(Release::Button(Button::Left)));
        t.apply(&Msg::Free(Release::Key("KeyA".into())));
        let rel = t.release_all();
        assert_eq!(rel.len(), 2);
        assert!(rel.contains(&Release::Key("ShiftLeft".into())));
        assert!(rel.contains(&Release::Button(Button::Left)));
        assert!(!rel.contains(&Release::Key("KeyA".into())), "A was already freed");
        assert!(t.is_empty(), "release_all clears");
        assert!(t.release_all().is_empty(), "and is idempotent");
    }

    #[test]
    fn a_drag_ends_before_the_modifiers_lift() {
        let mut t = HoldTracker::new();
        t.apply(&Msg::Hold(Release::Key("ShiftLeft".into())));
        t.apply(&Msg::Hold(Release::Button(Button::Left)));
        let rel = t.release_all();
        assert!(matches!(rel[0], Release::Button(_)), "buttons first: {rel:?}");
    }

    #[test]
    fn freeing_something_never_held_is_harmless() {
        let mut t = HoldTracker::new();
        t.apply(&Msg::Free(Release::Key("KeyA".into())));
        assert!(t.is_empty());
    }

    // ── the guard ──

    #[derive(Default)]
    struct Log {
        notices: Vec<Msg>,
        dead: bool,
    }
    struct FakeSink(std::rc::Rc<std::cell::RefCell<Log>>);
    impl HoldSink for FakeSink {
        fn notify(&mut self, m: &Msg) -> Result<(), ()> {
            let mut l = self.0.borrow_mut();
            if l.dead {
                return Err(());
            }
            l.notices.push(m.clone());
            Ok(())
        }
    }

    fn guarded() -> (GuardedAdapter<FakeInputAdapter, FakeSink>, std::rc::Rc<std::cell::RefCell<Log>>) {
        let log = std::rc::Rc::new(std::cell::RefCell::new(Log::default()));
        (GuardedAdapter::new(FakeInputAdapter::new(), FakeSink(log.clone())), log)
    }

    fn key(code: &str, down: bool) -> ValidatedEvent {
        validated(Action::Key { code: code.into(), down })
    }
    fn btn(down: bool) -> ValidatedEvent {
        validated(Action::Button { button: Button::Left, down, u: 0.5, v: 0.5 })
    }

    #[test]
    fn a_hold_is_announced_before_it_is_pressed_and_freed_after_it_is_released() {
        let (mut g, log) = guarded();
        g.inject(&key("ShiftLeft", true)).unwrap();
        assert_eq!(log.borrow().notices, vec![Msg::Hold(Release::Key("ShiftLeft".into()))]);
        assert_eq!(g.inner().injected().len(), 1);
        g.inject(&key("ShiftLeft", false)).unwrap();
        assert_eq!(log.borrow().notices.last(), Some(&Msg::Free(Release::Key("ShiftLeft".into()))));
        assert_eq!(g.inner().injected().len(), 2);
    }

    #[test]
    fn with_no_watchdog_a_key_down_is_refused_and_nothing_is_pressed() {
        let (mut g, log) = guarded();
        log.borrow_mut().dead = true;
        assert_eq!(g.inject(&key("ShiftLeft", true)), Err(AdapterError("watchdog_lost")));
        assert!(g.inner().injected().is_empty(), "no unprotected hold");
    }

    #[test]
    fn with_no_watchdog_a_button_down_is_refused_too() {
        let (mut g, log) = guarded();
        log.borrow_mut().dead = true;
        assert_eq!(g.inject(&btn(true)), Err(AdapterError("watchdog_lost")));
        assert!(g.inner().injected().is_empty());
    }

    #[test]
    fn releases_still_go_through_when_the_watchdog_is_gone() {
        // Refusing a release would be the one wrong direction.
        let (mut g, log) = guarded();
        g.inject(&key("ShiftLeft", true)).unwrap();
        log.borrow_mut().dead = true;
        assert!(g.inject(&key("ShiftLeft", false)).is_ok());
        assert!(g.release(&Release::Key("ShiftLeft".into())).is_ok());
        assert_eq!(g.inner().releases().len(), 1);
        assert_eq!(g.inner().injected().len(), 2, "down and up both happened");
    }

    #[test]
    fn moves_wheel_and_text_hold_nothing_so_they_need_no_watchdog() {
        let (mut g, log) = guarded();
        log.borrow_mut().dead = true;
        for a in [
            Action::MoveTo { u: 0.1, v: 0.1 },
            Action::Wheel { dx: 0.0, dy: 1.0, u: 0.5, v: 0.5 },
            Action::Text { text: "hi".into() },
        ] {
            assert!(g.inject(&validated(a)).is_ok());
        }
        assert!(log.borrow().notices.is_empty(), "nothing to announce");
    }

    #[test]
    fn a_press_the_inner_adapter_refused_is_freed_at_the_helper() {
        let (mut g, log) = guarded();
        g.inner.fail_inject = true;
        assert!(g.inject(&key("KeyA", true)).is_err());
        let n = log.borrow().notices.clone();
        assert_eq!(n, vec![Msg::Hold(Release::Key("KeyA".into())), Msg::Free(Release::Key("KeyA".into()))]);
    }

    #[test]
    fn release_goes_to_the_adapter_and_is_freed_at_the_helper() {
        let (mut g, log) = guarded();
        g.release(&Release::Button(Button::Left)).unwrap();
        assert_eq!(g.inner().releases().len(), 1);
        assert_eq!(log.borrow().notices, vec![Msg::Free(Release::Button(Button::Left))]);
    }

    #[test]
    fn the_guard_does_not_change_what_the_inner_adapter_supports_or_who_is_holding() {
        let (g, _) = guarded();
        assert!(g.supports_key("KeyA"));
        assert!(!g.physically_held("ShiftLeft"));
    }
}
