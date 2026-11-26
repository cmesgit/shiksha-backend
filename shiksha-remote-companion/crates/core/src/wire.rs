//! Typed controller input and semantic validation.
//!
//! JSON decoding lives in the transport crate (it needs serde). Everything that
//! decides whether a *decoded* value is acceptable lives here, so a decoder bug
//! can produce a wrong value but not a value that skips validation:
//! [`Envelope::validate`] is called again by the core before anything is queued.
//!
//! Source of truth: `realtime-event-contracts.md` and `contracts/input.schema.json`.
//! The reject vocabulary matches `contracts/fixtures.json`.

/// Frozen 101-entry DOM `KeyboardEvent.code` allowlist (RC-002).
pub const KEY_ALLOWLIST: [&str; 101] = [
    "KeyA", "KeyB", "KeyC", "KeyD", "KeyE", "KeyF", "KeyG", "KeyH", "KeyI", "KeyJ", "KeyK",
    "KeyL", "KeyM", "KeyN", "KeyO", "KeyP", "KeyQ", "KeyR", "KeyS", "KeyT", "KeyU", "KeyV",
    "KeyW", "KeyX", "KeyY", "KeyZ", "Digit0", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5",
    "Digit6", "Digit7", "Digit8", "Digit9", "Numpad0", "Numpad1", "Numpad2", "Numpad3",
    "Numpad4", "Numpad5", "Numpad6", "Numpad7", "Numpad8", "Numpad9", "F1", "F2", "F3", "F4",
    "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Backquote", "Minus", "Equal",
    "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period",
    "Slash", "IntlBackslash", "Space", "Enter", "Tab", "Backspace", "Delete", "Insert", "Home",
    "End", "PageUp", "PageDown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Escape",
    "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight",
    "MetaLeft", "MetaRight", "CapsLock", "NumLock", "NumpadAdd", "NumpadSubtract",
    "NumpadMultiply", "NumpadDivide", "NumpadDecimal", "NumpadEnter",
];

pub const MAX_SAFE_INT: u64 = 9_007_199_254_740_991;
pub const MAX_TEXT_SCALARS: usize = 256;
pub const MAX_TEXT_BYTES: usize = 1024;
pub const MAX_HEARTBEAT_KEYS: usize = 16;
pub const MAX_HEARTBEAT_BUTTONS: usize = 3;

pub const TOPIC_MOTION: &str = "shiksha.rc.motion.v1";
pub const TOPIC_INPUT: &str = "shiksha.rc.input.v1";
pub const TOPIC_STATUS: &str = "shiksha.rc.status.v1";

pub fn is_allowed_key(code: &str) -> bool {
    KEY_ALLOWLIST.contains(&code)
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum Button {
    Left,
    Middle,
    Right,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ResetReason {
    Blur,
    Visibility,
    PointerCancel,
    UserPause,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Body {
    Move { u: f64, v: f64 },
    Button { button: Button, down: bool, u: f64, v: f64, motion_seq: u64 },
    Wheel { dx: f64, dy: f64, u: f64, v: f64, motion_seq: u64 },
    Key { code: String, down: bool },
    Text { text: String },
    Reset { reason: ResetReason },
    Heartbeat { keys: Vec<String>, buttons: Vec<Button>, last_ack: u64 },
}

impl Body {
    /// Which lane carries it. Only `move` is lossy.
    pub fn is_motion(&self) -> bool {
        matches!(self, Body::Move { .. })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Envelope {
    pub grant_id: String,
    pub epoch: String,
    pub seq: u64,
    pub challenge: String,
    pub capture_generation: u64,
    pub body: Body,
}

/// Why a packet was refused. `reason()` is contract vocabulary shared with
/// `fixtures.json`; it is also safe to put in logs (it names no payload).
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Reject {
    BadUuid,
    BadSeq,
    BadChallenge,
    OutOfRange,
    UnsupportedKey,
    TooMany,
    TextTooLong,
    ControlChar,
}

impl Reject {
    pub fn reason(self) -> &'static str {
        match self {
            Reject::BadUuid => "bad_uuid",
            Reject::BadSeq => "bad_seq",
            Reject::BadChallenge => "bad_challenge",
            Reject::OutOfRange => "out_of_range",
            Reject::UnsupportedKey => "unsupported_key",
            Reject::TooMany => "too_many",
            Reject::TextTooLong => "text_too_long",
            Reject::ControlChar => "control_char",
        }
    }
}

/// Lowercase canonical `8-4-4-4-12` UUID.
pub fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 36 {
        return false;
    }
    for (i, c) in b.iter().enumerate() {
        let ok = match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        };
        if !ok {
            return false;
        }
    }
    true
}

/// 22 unpadded base64url characters (a 128-bit nonce).
pub fn is_challenge(s: &str) -> bool {
    s.len() == 22 && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

fn unit(x: f64) -> Result<(), Reject> {
    if x.is_finite() && (0.0..=1.0).contains(&x) {
        Ok(())
    } else {
        Err(Reject::OutOfRange)
    }
}

fn wheel(x: f64) -> Result<(), Reject> {
    if x.is_finite() && (-10.0..=10.0).contains(&x) {
        Ok(())
    } else {
        Err(Reject::OutOfRange)
    }
}

fn safe_int(n: u64) -> Result<(), Reject> {
    if n <= MAX_SAFE_INT {
        Ok(())
    } else {
        Err(Reject::OutOfRange)
    }
}

pub fn validate_text(text: &str) -> Result<(), Reject> {
    if text.chars().count() > MAX_TEXT_SCALARS || text.len() > MAX_TEXT_BYTES {
        return Err(Reject::TextTooLong);
    }
    // Control characters other than LF and TAB are refused; NUL is one of them.
    if text.chars().any(|c| c.is_control() && c != '\n' && c != '\t') {
        return Err(Reject::ControlChar);
    }
    Ok(())
}

impl Envelope {
    pub fn validate(&self) -> Result<(), Reject> {
        if !is_uuid(&self.grant_id) || !is_uuid(&self.epoch) {
            return Err(Reject::BadUuid);
        }
        if self.seq < 1 || self.seq > MAX_SAFE_INT {
            return Err(Reject::BadSeq);
        }
        if !is_challenge(&self.challenge) {
            return Err(Reject::BadChallenge);
        }
        match &self.body {
            Body::Move { u, v } => {
                unit(*u)?;
                unit(*v)
            }
            Body::Button { u, v, motion_seq, .. } => {
                unit(*u)?;
                unit(*v)?;
                safe_int(*motion_seq)
            }
            Body::Wheel { dx, dy, u, v, motion_seq } => {
                wheel(*dx)?;
                wheel(*dy)?;
                unit(*u)?;
                unit(*v)?;
                safe_int(*motion_seq)
            }
            Body::Key { code, .. } => {
                if is_allowed_key(code) {
                    Ok(())
                } else {
                    Err(Reject::UnsupportedKey)
                }
            }
            Body::Text { text } => validate_text(text),
            Body::Reset { .. } => Ok(()),
            Body::Heartbeat { keys, buttons, last_ack } => {
                if keys.len() > MAX_HEARTBEAT_KEYS || buttons.len() > MAX_HEARTBEAT_BUTTONS {
                    return Err(Reject::TooMany);
                }
                if keys.iter().any(|k| !is_allowed_key(k)) {
                    return Err(Reject::UnsupportedKey);
                }
                safe_int(*last_ack)
            }
        }
    }
}

/// Per-topic serialised ceilings. Applied by the decoder to the raw byte length
/// before any parsing, so an oversize packet costs one length compare.
pub fn topic_ceiling(topic: &str) -> Option<usize> {
    match topic {
        TOPIC_MOTION => Some(1200),
        TOPIC_INPUT | TOPIC_STATUS => Some(4096),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(body: Body) -> Envelope {
        Envelope {
            grant_id: "00000000-0000-4000-8000-000000000001".into(),
            epoch: "00000000-0000-4000-8000-000000000002".into(),
            seq: 12,
            challenge: "AAAAAAAAAAAAAAAAAAAAAA".into(),
            capture_generation: 3,
            body,
        }
    }

    #[test]
    fn allowlist_is_exactly_101_unique_entries() {
        let mut v = KEY_ALLOWLIST.to_vec();
        v.sort_unstable();
        v.dedup();
        assert_eq!(v.len(), 101);
    }

    #[test]
    fn never_injectable_keys_are_not_on_the_list() {
        // Ctrl+Alt+Delete and Win+L are blocked as *chords* by the executor; the
        // dedicated keys below must not be reachable at all.
        for k in ["PrintScreen", "ContextMenu", "AudioVolumeUp", "OSLeft", "Pause"] {
            assert!(!is_allowed_key(k), "{k}");
        }
    }

    #[test]
    fn valid_move_passes_and_nan_is_refused() {
        assert!(env(Body::Move { u: 0.25, v: 0.75 }).validate().is_ok());
        assert_eq!(env(Body::Move { u: f64::NAN, v: 0.5 }).validate(), Err(Reject::OutOfRange));
        assert_eq!(env(Body::Move { u: 1.0001, v: 0.5 }).validate(), Err(Reject::OutOfRange));
        assert_eq!(
            env(Body::Move { u: f64::INFINITY, v: 0.5 }).validate(),
            Err(Reject::OutOfRange)
        );
    }

    #[test]
    fn sequence_zero_and_overflow_are_bad_seq() {
        let mut e = env(Body::Reset { reason: ResetReason::Blur });
        e.seq = 0;
        assert_eq!(e.validate(), Err(Reject::BadSeq));
        e.seq = MAX_SAFE_INT + 1;
        assert_eq!(e.validate(), Err(Reject::BadSeq));
        e.seq = MAX_SAFE_INT;
        assert!(e.validate().is_ok());
    }

    #[test]
    fn uuid_must_be_lowercase_canonical() {
        let mut e = env(Body::Reset { reason: ResetReason::Blur });
        e.grant_id = "00000000-0000-4000-8000-00000000000A".into();
        assert_eq!(e.validate(), Err(Reject::BadUuid));
        e.grant_id = "0000000000004000800000000000000001".into();
        assert_eq!(e.validate(), Err(Reject::BadUuid));
    }

    #[test]
    fn text_limits_count_scalars_and_bytes_separately() {
        assert!(validate_text(&"a".repeat(256)).is_ok());
        assert_eq!(validate_text(&"a".repeat(257)), Err(Reject::TextTooLong));
        // 256 scalars but 4 bytes each = 1024 bytes: allowed. 255 + one 4-byte
        // scalar tips the byte ceiling only when the total exceeds 1024.
        assert!(validate_text(&"\u{1F600}".repeat(256)).is_ok());
        assert_eq!(validate_text(&format!("{}{}", "\u{1F600}".repeat(256), "a")), Err(Reject::TextTooLong));
        assert_eq!(validate_text("a\0b"), Err(Reject::ControlChar));
        assert_eq!(validate_text("a\x1bb"), Err(Reject::ControlChar));
        assert_eq!(validate_text("a\u{7f}"), Err(Reject::ControlChar));
        assert!(validate_text("line\n\ttab").is_ok());
    }

    #[test]
    fn heartbeat_limits() {
        let hb = |k: usize, b: usize| {
            env(Body::Heartbeat {
                keys: vec!["KeyA".into(); k],
                buttons: vec![Button::Left; b],
                last_ack: 0,
            })
        };
        assert!(hb(16, 3).validate().is_ok());
        assert_eq!(hb(17, 0).validate(), Err(Reject::TooMany));
        assert_eq!(hb(0, 4).validate(), Err(Reject::TooMany));
    }

    #[test]
    fn unknown_key_is_unsupported() {
        let e = env(Body::Key { code: "PrintScreen".into(), down: true });
        assert_eq!(e.validate(), Err(Reject::UnsupportedKey));
    }

    #[test]
    fn challenge_shape() {
        assert!(is_challenge("AAAAAAAAAAAAAAAAAAAAAA"));
        assert!(!is_challenge("AAAAAAAAAAAAAAAAAAAAA="));
        assert!(!is_challenge("short"));
    }
}
