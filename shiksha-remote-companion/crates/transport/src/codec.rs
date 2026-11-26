//! Strict JSON → [`Envelope`], and status encoding.
//!
//! Hand-written against `contracts/fixtures.json`. The reject vocabulary is the
//! contract's (`_reasons` in the fixtures file); `tests/fixtures.rs` runs every
//! shared vector through [`decode_input`] and fails if a valid one is refused or
//! an invalid one accepted.
//!
//! Order of checks is deliberate: byte ceiling first (one length compare, before
//! any allocation proportional to the input), then UTF-8, then JSON with
//! duplicate-key detection, then shape.

use std::collections::BTreeSet;
use std::fmt;

use serde::de::{Deserialize, Deserializer, Error as DeError, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};
use shiksha_companion_core::companion::Status;
use shiksha_companion_core::wire::{
    topic_ceiling, Body, Button, Envelope, Reject, ResetReason, MAX_SAFE_INT,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecodeError(pub &'static str);

impl DecodeError {
    pub fn reason(&self) -> &'static str {
        self.0
    }
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}

impl From<Reject> for DecodeError {
    fn from(r: Reject) -> Self {
        DecodeError(r.reason())
    }
}

// ── Duplicate-key-rejecting JSON ──────────────────────────────────────────

struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(StrictVisitor).map(Strict)
    }
}

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Value;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("any JSON value without duplicate object keys")
    }
    fn visit_bool<E: DeError>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }
    fn visit_i64<E: DeError>(self, v: i64) -> Result<Value, E> {
        Ok(Value::from(v))
    }
    fn visit_u64<E: DeError>(self, v: u64) -> Result<Value, E> {
        Ok(Value::from(v))
    }
    fn visit_f64<E: DeError>(self, v: f64) -> Result<Value, E> {
        // serde_json never produces NaN/Infinity from text, but refuse anyway.
        serde_json::Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite number"))
    }
    fn visit_str<E: DeError>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_owned()))
    }
    fn visit_unit<E: DeError>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut out = Vec::new();
        while let Some(Strict(v)) = seq.next_element()? {
            out.push(v);
        }
        Ok(Value::Array(out))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut out = Map::new();
        while let Some((k, Strict(v))) = map.next_entry::<String, Strict>()? {
            if out.contains_key(&k) {
                return Err(<A::Error as DeError>::custom("duplicate key"));
            }
            out.insert(k, v);
        }
        Ok(Value::Object(out))
    }
}

/// Parse UTF-8 JSON, refusing duplicate keys and trailing garbage.
pub fn parse_strict(bytes: &[u8]) -> Result<Value, DecodeError> {
    let text = std::str::from_utf8(bytes).map_err(|_| DecodeError("bad_encoding"))?;
    let mut de = serde_json::Deserializer::from_str(text);
    let Strict(v) = Strict::deserialize(&mut de).map_err(|_| DecodeError("bad_json"))?;
    de.end().map_err(|_| DecodeError("bad_json"))?;
    Ok(v)
}

// ── Envelope ──────────────────────────────────────────────────────────────

const ENVELOPE_FIELDS: [&str; 8] =
    ["v", "grant_id", "epoch", "seq", "challenge", "capture_generation", "kind", "body"];

fn only_fields(obj: &Map<String, Value>, allowed: &[&str]) -> Result<(), DecodeError> {
    let allowed: BTreeSet<&str> = allowed.iter().copied().collect();
    if obj.keys().any(|k| !allowed.contains(k.as_str())) {
        return Err(DecodeError("unknown_field"));
    }
    Ok(())
}

fn need<'a>(obj: &'a Map<String, Value>, k: &str) -> Result<&'a Value, DecodeError> {
    obj.get(k).ok_or(DecodeError("missing_field"))
}

fn num(v: &Value) -> Result<f64, DecodeError> {
    v.as_f64().ok_or(DecodeError("out_of_range"))
}

fn uint(v: &Value, reason: &'static str) -> Result<u64, DecodeError> {
    // `as_u64` is None for floats such as 1.0 and for negatives, which is the
    // behaviour the contract wants ("not an integer").
    let n = v.as_u64().ok_or(DecodeError(reason))?;
    if n > MAX_SAFE_INT {
        return Err(DecodeError(reason));
    }
    Ok(n)
}

fn string(v: &Value, reason: &'static str) -> Result<String, DecodeError> {
    v.as_str().map(str::to_owned).ok_or(DecodeError(reason))
}

fn boolean(v: &Value) -> Result<bool, DecodeError> {
    v.as_bool().ok_or(DecodeError("out_of_range"))
}

fn button(v: &Value) -> Result<Button, DecodeError> {
    match v.as_str() {
        Some("left") => Ok(Button::Left),
        Some("middle") => Ok(Button::Middle),
        Some("right") => Ok(Button::Right),
        _ => Err(DecodeError("out_of_range")),
    }
}

fn body_from(kind: &str, body: &Value) -> Result<Body, DecodeError> {
    let obj = body.as_object().ok_or(DecodeError("bad_body"))?;
    match kind {
        "move" => {
            only_fields(obj, &["u", "v"])?;
            Ok(Body::Move { u: num(need(obj, "u")?)?, v: num(need(obj, "v")?)? })
        }
        "button" => {
            only_fields(obj, &["button", "down", "u", "v", "motion_seq"])?;
            Ok(Body::Button {
                button: button(need(obj, "button")?)?,
                down: boolean(need(obj, "down")?)?,
                u: num(need(obj, "u")?)?,
                v: num(need(obj, "v")?)?,
                motion_seq: uint(need(obj, "motion_seq")?, "out_of_range")?,
            })
        }
        "wheel" => {
            only_fields(obj, &["dx", "dy", "u", "v", "motion_seq"])?;
            Ok(Body::Wheel {
                dx: num(need(obj, "dx")?)?,
                dy: num(need(obj, "dy")?)?,
                u: num(need(obj, "u")?)?,
                v: num(need(obj, "v")?)?,
                motion_seq: uint(need(obj, "motion_seq")?, "out_of_range")?,
            })
        }
        "key" => {
            only_fields(obj, &["code", "down"])?;
            Ok(Body::Key {
                code: string(need(obj, "code")?, "unsupported_key")?,
                down: boolean(need(obj, "down")?)?,
            })
        }
        "text" => {
            only_fields(obj, &["text"])?;
            Ok(Body::Text { text: string(need(obj, "text")?, "out_of_range")? })
        }
        "reset" => {
            only_fields(obj, &["reason"])?;
            let reason = match need(obj, "reason")?.as_str() {
                Some("blur") => ResetReason::Blur,
                Some("visibility") => ResetReason::Visibility,
                Some("pointer_cancel") => ResetReason::PointerCancel,
                Some("user_pause") => ResetReason::UserPause,
                _ => return Err(DecodeError("out_of_range")),
            };
            Ok(Body::Reset { reason })
        }
        "heartbeat" => {
            only_fields(obj, &["keys", "buttons", "last_ack"])?;
            let keys = need(obj, "keys")?.as_array().ok_or(DecodeError("bad_body"))?;
            let buttons = need(obj, "buttons")?.as_array().ok_or(DecodeError("bad_body"))?;
            // Count before converting, so a 10 000-element array is `too_many`
            // rather than an allocation.
            if keys.len() > 16 || buttons.len() > 3 {
                return Err(DecodeError("too_many"));
            }
            Ok(Body::Heartbeat {
                keys: keys
                    .iter()
                    .map(|k| string(k, "unsupported_key"))
                    .collect::<Result<_, _>>()?,
                buttons: buttons.iter().map(button).collect::<Result<_, _>>()?,
                last_ack: uint(need(obj, "last_ack")?, "out_of_range")?,
            })
        }
        _ => Err(DecodeError("unknown_kind")),
    }
}

/// Decode and fully validate one controller packet from `topic`.
pub fn decode_input(topic: &str, raw: &[u8]) -> Result<Envelope, DecodeError> {
    let ceiling = topic_ceiling(topic).ok_or(DecodeError("oversize"))?;
    if raw.len() > ceiling {
        return Err(DecodeError("oversize"));
    }
    let value = parse_strict(raw)?;
    let obj = value.as_object().ok_or(DecodeError("bad_body"))?;
    only_fields(obj, &ENVELOPE_FIELDS)?;
    for f in ENVELOPE_FIELDS {
        if !obj.contains_key(f) {
            return Err(DecodeError("missing_field"));
        }
    }
    if obj["v"].as_u64() != Some(1) {
        return Err(DecodeError("bad_version"));
    }
    let kind = obj["kind"].as_str().ok_or(DecodeError("unknown_kind"))?;
    let body = body_from(kind, &obj["body"])?;
    let env = Envelope {
        grant_id: string(&obj["grant_id"], "bad_uuid")?,
        epoch: string(&obj["epoch"], "bad_uuid")?,
        seq: uint(&obj["seq"], "bad_seq")?,
        challenge: string(&obj["challenge"], "bad_challenge")?,
        capture_generation: uint(&obj["capture_generation"], "out_of_range")?,
        body,
    };
    // The core validates again before queueing; doing it here as well means a
    // decoder bug can only ever be *stricter*.
    env.validate()?;
    Ok(env)
}

// ── Status encoding (native → controller) ─────────────────────────────────

/// `shiksha.rc.status.v1` payload bytes. Never contains input contents.
pub fn encode_status(grant_id: &str, epoch: &str, status: &Status) -> Vec<u8> {
    let (kind, body) = match status {
        Status::Ready { capture_generation, track_sid } => (
            "ready",
            serde_json::json!({"capture_generation": capture_generation, "track_sid": track_sid}),
        ),
        Status::Challenge { nonce, valid_for_ms } => (
            "challenge",
            serde_json::json!({"nonce": nonce, "valid_for_ms": valid_for_ms}),
        ),
        Status::Ack { input_seq, motion_seq } => (
            "ack",
            serde_json::json!({"input_seq": input_seq, "motion_seq": motion_seq}),
        ),
        Status::Stopped { reason } => ("stopped", serde_json::json!({"reason": reason})),
        Status::Error { code } => ("error", serde_json::json!({"code": code})),
    };
    serde_json::to_vec(&serde_json::json!({
        "v": 1, "grant_id": grant_id, "epoch": epoch, "kind": kind, "body": body
    }))
    .expect("a json! value always serialises")
}

/// Where status goes. The one method takes exactly one identity, so there is no
/// way to express a room broadcast through this trait. An empty identity is an
/// error, never "everyone" (realtime-event-contracts.md §LiveKit transport).
pub trait StatusSink {
    fn send_to(&self, identity: &str, topic: &str, payload: Vec<u8>) -> Result<(), SinkError>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum SinkError {
    EmptyDestination,
    Transport(String),
}

pub fn deliver(
    sink: &dyn StatusSink,
    to_identity: &str,
    grant_id: &str,
    epoch: &str,
    status: &Status,
) -> Result<(), SinkError> {
    if to_identity.is_empty() {
        return Err(SinkError::EmptyDestination);
    }
    let payload = encode_status(grant_id, epoch, status);
    sink.send_to(to_identity, shiksha_companion_core::wire::TOPIC_STATUS, payload)
}
