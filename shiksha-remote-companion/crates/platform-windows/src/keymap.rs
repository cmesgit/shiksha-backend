//! DOM `KeyboardEvent.code` → Windows set-1 scan code.
//!
//! The protocol sends *physical* key identities, not characters or virtual-key
//! integers ("no OS virtual-key integers from network"). Scan codes are the
//! physical-position half of that: `SendInput` with `KEYEVENTF_SCANCODE` presses
//! "the key at this position" and the host's own layout turns it into a
//! character. That is what makes a US-position `KeyQ` type `a` on an AZERTY host,
//! which is correct for a remote desktop and is why layout is *shown*, not
//! translated.
//!
//! Extended keys carry an `E0` prefix in set 1; here that is bit 8 of the `u16`
//! (`0xE0xx`), and `SendInput` wants it as `KEYEVENTF_EXTENDEDKEY` with the low
//! byte as `wScan`.
//!
//! **Compile-time guarantees** (the `const _: () = assert!` blocks at the bottom
//! run inside rustc, so they hold even where no test binary may execute):
//! every one of the 101 allowlisted codes has a scan code, and no two codes share
//! one. A missing entry is a build failure, not a key that silently does nothing.

use shiksha_companion_core::wire::KEY_ALLOWLIST;

/// `(DOM code, scan code)`. `0xE0xx` marks an extended key.
pub const KEYMAP: [(&str, u16); 101] = [
    ("Escape", 0x01),
    ("Digit1", 0x02), ("Digit2", 0x03), ("Digit3", 0x04), ("Digit4", 0x05),
    ("Digit5", 0x06), ("Digit6", 0x07), ("Digit7", 0x08), ("Digit8", 0x09),
    ("Digit9", 0x0A), ("Digit0", 0x0B),
    ("Minus", 0x0C), ("Equal", 0x0D), ("Backspace", 0x0E), ("Tab", 0x0F),
    ("KeyQ", 0x10), ("KeyW", 0x11), ("KeyE", 0x12), ("KeyR", 0x13), ("KeyT", 0x14),
    ("KeyY", 0x15), ("KeyU", 0x16), ("KeyI", 0x17), ("KeyO", 0x18), ("KeyP", 0x19),
    ("BracketLeft", 0x1A), ("BracketRight", 0x1B), ("Enter", 0x1C),
    ("ControlLeft", 0x1D),
    ("KeyA", 0x1E), ("KeyS", 0x1F), ("KeyD", 0x20), ("KeyF", 0x21), ("KeyG", 0x22),
    ("KeyH", 0x23), ("KeyJ", 0x24), ("KeyK", 0x25), ("KeyL", 0x26),
    ("Semicolon", 0x27), ("Quote", 0x28), ("Backquote", 0x29),
    ("ShiftLeft", 0x2A), ("Backslash", 0x2B),
    ("KeyZ", 0x2C), ("KeyX", 0x2D), ("KeyC", 0x2E), ("KeyV", 0x2F), ("KeyB", 0x30),
    ("KeyN", 0x31), ("KeyM", 0x32),
    ("Comma", 0x33), ("Period", 0x34), ("Slash", 0x35), ("ShiftRight", 0x36),
    ("NumpadMultiply", 0x37), ("AltLeft", 0x38), ("Space", 0x39), ("CapsLock", 0x3A),
    ("F1", 0x3B), ("F2", 0x3C), ("F3", 0x3D), ("F4", 0x3E), ("F5", 0x3F),
    ("F6", 0x40), ("F7", 0x41), ("F8", 0x42), ("F9", 0x43), ("F10", 0x44),
    ("NumLock", 0x45),
    ("Numpad7", 0x47), ("Numpad8", 0x48), ("Numpad9", 0x49), ("NumpadSubtract", 0x4A),
    ("Numpad4", 0x4B), ("Numpad5", 0x4C), ("Numpad6", 0x4D), ("NumpadAdd", 0x4E),
    ("Numpad1", 0x4F), ("Numpad2", 0x50), ("Numpad3", 0x51), ("Numpad0", 0x52),
    ("NumpadDecimal", 0x53),
    ("IntlBackslash", 0x56), ("F11", 0x57), ("F12", 0x58),
    // Extended (E0-prefixed) keys.
    ("NumpadEnter", 0xE01C), ("ControlRight", 0xE01D), ("NumpadDivide", 0xE035),
    ("AltRight", 0xE038),
    ("Home", 0xE047), ("ArrowUp", 0xE048), ("PageUp", 0xE049), ("ArrowLeft", 0xE04B),
    ("ArrowRight", 0xE04D), ("End", 0xE04F), ("ArrowDown", 0xE050),
    ("PageDown", 0xE051), ("Insert", 0xE052), ("Delete", 0xE053),
    ("MetaLeft", 0xE05B), ("MetaRight", 0xE05C),
];

/// A decoded scan code ready for `INPUT`.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct Scan {
    /// Low byte, goes in `wScan`.
    pub code: u16,
    /// Set `KEYEVENTF_EXTENDEDKEY`.
    pub extended: bool,
}

const fn bytes_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut i = 0;
    while i < a.len() {
        if a[i] != b[i] {
            return false;
        }
        i += 1;
    }
    true
}

/// `const` so the build itself can check coverage.
pub const fn lookup(code: &str) -> Option<u16> {
    let mut i = 0;
    while i < KEYMAP.len() {
        if bytes_eq(KEYMAP[i].0.as_bytes(), code.as_bytes()) {
            return Some(KEYMAP[i].1);
        }
        i += 1;
    }
    None
}

pub fn scan_for(code: &str) -> Option<Scan> {
    lookup(code).map(|raw| Scan { code: raw & 0xFF, extended: raw & 0xFF00 == 0xE000 })
}

// ── compile-time proofs ───────────────────────────────────────────────────

/// Every allowlisted DOM code has a scan code.
const fn covers_allowlist() -> bool {
    let mut i = 0;
    while i < KEY_ALLOWLIST.len() {
        if lookup(KEY_ALLOWLIST[i]).is_none() {
            return false;
        }
        i += 1;
    }
    true
}

/// No two entries share a DOM code or a scan code.
const fn no_duplicates() -> bool {
    let mut i = 0;
    while i < KEYMAP.len() {
        let mut j = i + 1;
        while j < KEYMAP.len() {
            if KEYMAP[i].1 == KEYMAP[j].1 || bytes_eq(KEYMAP[i].0.as_bytes(), KEYMAP[j].0.as_bytes()) {
                return false;
            }
            j += 1;
        }
        i += 1;
    }
    true
}

/// The map contains nothing the protocol does not allow (so nothing in it can be
/// reached from the network that the contract did not name).
const fn nothing_extra() -> bool {
    let mut i = 0;
    while i < KEYMAP.len() {
        let mut found = false;
        let mut j = 0;
        while j < KEY_ALLOWLIST.len() {
            if bytes_eq(KEYMAP[i].0.as_bytes(), KEY_ALLOWLIST[j].as_bytes()) {
                found = true;
            }
            j += 1;
        }
        if !found {
            return false;
        }
        i += 1;
    }
    true
}

const _: () = assert!(covers_allowlist(), "a DOM code on the protocol allowlist has no scan code");
const _: () = assert!(no_duplicates(), "two keys share a DOM code or a scan code");
const _: () = assert!(nothing_extra(), "the key map holds a code the protocol does not allow");
// Spot checks of the two places a table like this is most often wrong.
const _: () = assert!(matches!(lookup("KeyA"), Some(0x1E)));
const _: () = assert!(matches!(lookup("ArrowUp"), Some(0xE048)), "arrows are E0-prefixed");
const _: () = assert!(matches!(lookup("Numpad8"), Some(0x48)), "numpad 8 shares ArrowUp's low byte, without E0");
const _: () = assert!(lookup("PrintScreen").is_none());

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extended_keys_are_flagged_and_their_low_byte_is_the_scan_code() {
        assert_eq!(scan_for("ArrowUp"), Some(Scan { code: 0x48, extended: true }));
        assert_eq!(scan_for("Numpad8"), Some(Scan { code: 0x48, extended: false }));
        assert_eq!(scan_for("ControlRight"), Some(Scan { code: 0x1D, extended: true }));
        assert_eq!(scan_for("ControlLeft"), Some(Scan { code: 0x1D, extended: false }));
        assert_eq!(scan_for("NumpadEnter"), Some(Scan { code: 0x1C, extended: true }));
        assert_eq!(scan_for("Enter"), Some(Scan { code: 0x1C, extended: false }));
    }

    #[test]
    fn unknown_and_never_injectable_keys_have_no_scan_code() {
        for k in ["PrintScreen", "Pause", "ContextMenu", "ScrollLock", "", "keya"] {
            assert_eq!(scan_for(k), None, "{k:?}");
        }
    }

    #[test]
    fn the_table_has_one_entry_per_allowlisted_code() {
        assert_eq!(KEYMAP.len(), KEY_ALLOWLIST.len());
    }
}
