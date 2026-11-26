//! `shiksha-companion-watchdog`: releases held keys and buttons if the companion
//! dies.
//!
//! Reads hold notices from stdin (a pipe whose only write end the companion holds).
//! When the pipe closes — because the companion exited or was killed — it releases
//! everything it was told is held, then exits. See
//! `platform-windows/src/watchdog.rs` for the protocol and the reasoning.
//!
//! What it must never be: a network client, an input *source*, or something that
//! can open the gate. It has no dependencies beyond the platform crate, opens no
//! socket, and the only thing it ever sends to the OS is a key-up or button-up for
//! something it was told the companion pressed.
//!
//! NOT RUN. T09 (kill the companion while holding Shift) has never been exercised
//! against this binary, so held-input release is not certified.

#![cfg_attr(windows, windows_subsystem = "windows")]

use std::io::{BufRead, Read};

use shiksha_companion_platform_windows::watchdog::{parse_line, HoldTracker, Msg};

/// A line longer than this is garbage. Reading is bounded so a corrupt stream
/// cannot grow this process without limit.
const MAX_LINE: u64 = 256;

fn main() {
    let mut tracker = HoldTracker::new();
    let stdin = std::io::stdin();
    let mut reader = stdin.lock();
    let mut buf = Vec::with_capacity(MAX_LINE as usize);

    loop {
        buf.clear();
        let n = match reader.by_ref().take(MAX_LINE).read_until(b'\n', &mut buf) {
            Ok(n) => n,
            Err(_) => break, // a broken pipe is the death notice
        };
        if n == 0 {
            break; // end of file: the companion is gone
        }
        if buf.last() != Some(&b'\n') && n as u64 == MAX_LINE {
            // Over-long line: discard the rest of it, act on nothing.
            let mut sink = Vec::new();
            loop {
                sink.clear();
                match reader.by_ref().take(MAX_LINE).read_until(b'\n', &mut sink) {
                    Ok(0) | Err(_) => break,
                    Ok(_) if sink.last() == Some(&b'\n') => break,
                    Ok(_) => {}
                }
            }
            continue;
        }
        match std::str::from_utf8(&buf).ok().map(parse_line) {
            Some(Ok(Msg::Quit)) => break,
            Some(Ok(m)) => tracker.apply(&m),
            // Unparseable: ignore. Never release on a guess, and never stop on one.
            _ => {}
        }
    }

    release_everything(&mut tracker);
}

#[cfg(windows)]
fn release_everything(tracker: &mut HoldTracker) {
    use shiksha_companion_platform_windows::input::{button_flag, key_input, mouse, InputSink, Win32Sink};
    use shiksha_companion_platform_windows::keymap::scan_for;
    use shiksha_companion_platform_windows::watchdog::Release;

    let mut sink = Win32Sink;
    for what in tracker.release_all() {
        let input = match &what {
            Release::Key(code) => match scan_for(code) {
                Some(scan) => key_input(scan, false),
                None => continue,
            },
            Release::Button(b) => mouse(button_flag(*b, false), 0, 0, 0),
        };
        // One retry: a transient failure here would leave a stuck key.
        if sink.send(&[input]).is_err() {
            let _ = sink.send(&[input]);
        }
    }
}

#[cfg(not(windows))]
fn release_everything(tracker: &mut HoldTracker) {
    // Nothing to release on a platform with no Windows adapter.
    let _ = tracker.release_all();
}
