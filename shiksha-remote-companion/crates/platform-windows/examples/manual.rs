//! Hands-on checks that need a real desktop and a real person. Windows x64 only.
//!
//! Nothing here talks to a server. Each mode exercises the real Win32 code (windows,
//! hotkey, hooks, SendInput, the helper process) against the real core types, and
//! prints PASS / FAIL / INCONCLUSIVE with what to look at.
//!
//! ```text
//! cargo build -p shiksha-companion-watchdog
//! cargo run -p shiksha-companion-platform-windows --example manual -- <mode>
//!
//! status             status window + Stop chord: press the chord, click Stop
//! consent-injected   T13: injected input tries to approve; HANDS OFF for ~35 s
//! consent-physical   you click Allow with the mouse after the dwell
//! consent-keyboard   you Tab to Allow and press Space after the dwell
//! t09                T09: hold Shift by injection, then kill this process
//! t09-chord          hold Shift by injection, then press the Stop chord
//! ```
//!
//! Safety: the only synthetic input is Tab/Space (consent-injected), and Shift/left
//! button held down (t09). If Shift ever sticks, tap Shift once on the real keyboard.

#[cfg(not(all(windows, target_pointer_width = "64")))]
fn main() {
    eprintln!("Windows x64 only");
}

#[cfg(all(windows, target_pointer_width = "64"))]
fn main() {
    real::main()
}

#[cfg(all(windows, target_pointer_width = "64"))]
mod real {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use shiksha_companion_core::adapter::test_support::validated;
    use shiksha_companion_core::adapter::{Action, InputAdapter};
    use shiksha_companion_core::clock::SystemClock;
    use shiksha_companion_core::companion::StopLatch;
    use shiksha_companion_core::consent::{ControlPrompt, Outcome, RecordingStatus};
    use shiksha_companion_platform_windows::display::{enumerate_displays, virtual_desktop_bounds};
    use shiksha_companion_platform_windows::emergency::EmergencyStop;
    use shiksha_companion_platform_windows::grant::{AskError, GrantKit};
    use shiksha_companion_platform_windows::input::{key_input, InputSink, Win32Sink, WindowsInput};
    use shiksha_companion_platform_windows::keymap::scan_for;
    use shiksha_companion_platform_windows::provenance::PhysicalState;
    use shiksha_companion_platform_windows::watchdog::{GuardedAdapter, PipeHoldSink};
    use shiksha_companion_platform_windows::window::StatusWindow;

    pub fn main() {
        let mode = std::env::args().nth(1).unwrap_or_default();
        match mode.as_str() {
            "status" => status(),
            "consent-injected" => consent(Who::Injected),
            "consent-physical" => consent(Who::Mouse),
            "consent-keyboard" => consent(Who::Keyboard),
            "t09" => t09(false),
            "t09-chord" => t09(true),
            _ => eprintln!("modes: status | consent-injected | consent-physical | consent-keyboard | t09 | t09-chord"),
        }
    }

    fn verdict(ok: bool, what: &str) {
        println!("{}  {what}", if ok { "PASS" } else { "FAIL" });
    }

    fn prompt() -> ControlPrompt {
        ControlPrompt {
            controller_name: "Test Teacher (manual harness)".into(),
            controller_context: "Teacher".into(),
            meeting_title: "Manual test".into(),
            device_name: "this computer".into(),
            display_label: "Display 1".into(),
            capabilities: vec!["pointer.move".into(), "pointer.button".into(), "keyboard.physical".into()],
            stop_shortcut: String::new(), // overwritten with the real chord
            recording: RecordingStatus::Known { recording: false },
            public_broadcast: false,
        }
    }

    // ── status ───────────────────────────────────────────────────────────

    fn status() {
        let latch = StopLatch::new();
        let chord = match EmergencyStop::register_default(latch.clone()) {
            Ok(c) => c,
            Err(e) => return println!("FAIL  chord did not register: {e:?}"),
        };
        println!("Chord registered: {}", chord.shortcut_text());
        let win = match StatusWindow::open(latch.clone(), "Remote control active\nTest Teacher".into()) {
            Ok(w) => w,
            Err(e) => return println!("FAIL  status window did not open: {e:?}"),
        };
        println!("Status window is open (top right). Look for: always on top, does NOT steal focus");
        println!("from the window you were using, no close button, Alt+F4 does nothing to it.");
        println!("Now press {} and click the Stop button. 60 s.", chord.shortcut_text());
        let g0 = latch.generation();
        let end = Instant::now() + Duration::from_secs(60);
        let (mut got_chord, mut got_btn) = (false, false);
        while Instant::now() < end {
            if chord.take_fired() && !got_chord {
                got_chord = true;
                println!("  chord fired; latch generation {} -> {}", g0, latch.generation());
            }
            if win.take_stop_pressed() && !got_btn {
                got_btn = true;
                println!("  Stop button pressed; latch generation now {}", latch.generation());
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        verdict(got_chord, "chord closed the latch");
        verdict(got_btn, "Stop button closed the latch");
        verdict(latch.generation() > g0, "latch generation advanced");
    }

    // ── consent (T13 and the happy paths) ────────────────────────────────

    #[derive(Copy, Clone, PartialEq)]
    enum Who {
        Injected,
        Mouse,
        Keyboard,
    }

    fn send_keys(codes: &[&str]) {
        let mut sink = Win32Sink;
        for c in codes {
            if let Some(s) = scan_for(c) {
                let _ = sink.send(&[key_input(s, true)]);
                std::thread::sleep(Duration::from_millis(30));
                let _ = sink.send(&[key_input(s, false)]);
                std::thread::sleep(Duration::from_millis(60));
            }
        }
    }

    fn consent(who: Who) {
        let latch = StopLatch::new();
        let physical = Arc::new(PhysicalState::new());
        let kit = match GrantKit::new(latch, physical) {
            Ok(k) => k,
            Err(e) => return println!("FAIL  chord did not register: {e:?}"),
        };
        println!("Chord: {}", kit.stop_shortcut());
        match who {
            Who::Injected => {
                println!("T13: HANDS OFF mouse and keyboard until this finishes (~35 s).");
                println!("Synthetic Tab+Space will try to press Allow after the dwell, 6 times.");
                std::thread::spawn(|| {
                    std::thread::sleep(Duration::from_millis(4500)); // past the 3 s dwell
                    for _ in 0..6 {
                        send_keys(&["Tab", "Space"]);
                        std::thread::sleep(Duration::from_millis(700));
                    }
                });
            }
            Who::Mouse => println!("Wait for Allow to enable (about 3 s), then CLICK it with the mouse."),
            Who::Keyboard => println!("Wait for Allow to enable, press Tab to reach it, then Space."),
        }

        let started = Instant::now();
        let r = kit.ask(SystemClock::new(), prompt(), false);
        println!("dialog closed after {:.1} s", started.elapsed().as_secs_f32());

        match (who, r) {
            (Who::Injected, Err(AskError::Declined(o))) => {
                verdict(true, &format!("injected input did NOT approve (outcome {o:?})"));
                println!("      Look above: did the dialog show Allow enabled and stay un-approved? That is the pass.");
            }
            (Who::Injected, Ok(a)) => {
                verdict(false, "INJECTED INPUT APPROVED THE DIALOG");
                drop(a);
            }
            (Who::Injected, Err(e)) => println!("INCONCLUSIVE  {e:?}"),
            (_, Ok(a)) => {
                println!("approved; refused injected/early attempts: {}", a.refused_approvals);
                verdict(a.refused_approvals == 0, "a real click/keypress approved on the first try");
                verdict(
                    a.decision.capabilities == ["pointer.move", "pointer.button", "keyboard.physical"],
                    "decision carries exactly the capabilities shown",
                );
                println!("status window is up for 6 s (guard alive):");
                a.guard.arm();
                let end = Instant::now() + Duration::from_secs(6);
                while Instant::now() < end {
                    for ev in a.guard.poll() {
                        println!("  guard event: {ev:?}");
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                println!("(touch the mouse during those 6 s to see PhysicalActivity; that is expected)");
            }
            (_, Err(AskError::Declined(Outcome::Denied(why)))) => println!("INCONCLUSIVE  you denied/timed out: {why:?}"),
            (_, Err(e)) => println!("INCONCLUSIVE  {e:?}"),
        }
    }

    // ── T09 ──────────────────────────────────────────────────────────────

    fn t09(chord_mode: bool) {
        let exe = std::env::current_exe().expect("exe path");
        // target\debug\examples\manual.exe -> target\debug\
        let helper = exe.parent().and_then(|p| p.parent()).map(|p| p.join("shiksha-companion-watchdog.exe"));
        let Some(helper) = helper.filter(|p| p.exists()) else {
            return println!("FAIL  helper not found; run: cargo build -p shiksha-companion-watchdog");
        };
        let sink = match PipeHoldSink::spawn(&helper) {
            Ok(s) => s,
            Err(e) => return println!("FAIL  could not start helper: {e}"),
        };
        let displays = match enumerate_displays() {
            Ok(d) if !d.is_empty() => d,
            other => return println!("FAIL  no displays: {other:?}"),
        };
        let physical = Arc::new(PhysicalState::new());
        let inner = WindowsInput::new(Win32Sink, physical, displays[0].clone(), virtual_desktop_bounds());
        let mut adapter = GuardedAdapter::new(inner, sink);

        let latch = StopLatch::new();
        let chord = EmergencyStop::register_default(latch.clone()).ok();

        let r = adapter.inject(&validated(Action::Key { code: "ShiftLeft".into(), down: true }));
        println!("hold Shift by injection: {r:?}");
        if r.is_err() {
            return println!("INCONCLUSIVE  could not even press Shift");
        }
        let pid = std::process::id();
        println!("\nLEFT SHIFT IS NOW HELD DOWN BY THIS PROGRAM (pid {pid}).");
        if chord_mode {
            println!("Press {} now. This program should release Shift itself.", chord.as_ref().map_or("(no chord)".into(), |c| c.shortcut_text()));
        } else {
            println!("In ANOTHER terminal run:   taskkill /F /PID {pid}");
            println!("The helper process should release Shift within about a second.");
        }
        println!("Then open Notepad and type some letters.");
        println!("  PASS = lowercase, no stuck Shift.   FAIL = capitals / Shift still down (tap real Shift to clear).\n");

        // Keep the process alive. In chord mode the app's own cleanup is the thing under test.
        let g0 = latch.generation();
        loop {
            std::thread::sleep(Duration::from_millis(50));
            if chord_mode && latch.generation() > g0 {
                let r = adapter.release(&shiksha_companion_core::adapter::Release::Key("ShiftLeft".into()));
                println!("chord pressed; release() -> {r:?}. Now type in Notepad: lowercase = PASS.");
                std::thread::sleep(Duration::from_secs(20));
                return;
            }
        }
    }
}
