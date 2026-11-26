//! Runs the shared v1 fixtures (RC-002) through the Rust decoder.
//!
//! "A validator that rejects a valid vector, or accepts an invalid one, is
//! non-conforming - do not fix the fixture to match an implementation."

use serde_json::Value;
use shiksha_companion_transport::codec::decode_input;

const FIXTURES: &str = include_str!(
    "../../../../docs/architecture/shiksha-remote/contracts/fixtures.json"
);

fn fixtures() -> Value {
    serde_json::from_str(FIXTURES).expect("fixtures.json parses")
}

#[test]
fn every_valid_vector_is_accepted() {
    let f = fixtures();
    let valid = f["valid"].as_array().unwrap();
    assert!(!valid.is_empty());
    for v in valid {
        let name = v["name"].as_str().unwrap();
        let raw = serde_json::to_vec(&v["message"]).unwrap();
        // Motion vectors ride the motion topic; everything else the input topic.
        let topic = if v["message"]["kind"] == "move" {
            "shiksha.rc.motion.v1"
        } else {
            "shiksha.rc.input.v1"
        };
        decode_input(topic, &raw).unwrap_or_else(|e| panic!("valid vector {name} refused: {e}"));
    }
}

#[test]
fn every_invalid_vector_is_refused_with_the_stated_reason() {
    let f = fixtures();
    let invalid = f["invalid"].as_array().unwrap();
    assert!(!invalid.is_empty());
    for v in invalid {
        let name = v["name"].as_str().unwrap();
        let want = v["reason"].as_str().unwrap();
        let raw = serde_json::to_vec(&v["message"]).unwrap();
        let topic = if v["message"]["kind"] == "move" {
            "shiksha.rc.motion.v1"
        } else {
            "shiksha.rc.input.v1"
        };
        match decode_input(topic, &raw) {
            Ok(_) => panic!("invalid vector {name} was accepted"),
            Err(e) => assert_eq!(e.reason(), want, "vector {name}"),
        }
    }
}

#[test]
fn every_raw_vector_is_refused_with_the_stated_reason() {
    let f = fixtures();
    for v in f["invalid_raw"].as_array().unwrap() {
        let name = v["name"].as_str().unwrap();
        let want = v["reason"].as_str().unwrap();
        let topic = v["topic"].as_str().unwrap();
        // Raw vectors carry a UTF-8 string ("raw") or hex bytes ("raw_hex").
        let bytes: Vec<u8> = if let Some(s) = v.get("raw").and_then(Value::as_str) {
            s.as_bytes().to_vec()
        } else if let Some(h) = v.get("raw_hex").and_then(Value::as_str) {
            (0..h.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&h[i..i + 2], 16).expect("hex"))
                .collect()
        } else {
            panic!("vector {name} has neither raw nor raw_hex");
        };
        match decode_input(topic, &bytes) {
            Ok(_) => panic!("raw vector {name} was accepted"),
            Err(e) => assert_eq!(e.reason(), want, "vector {name}"),
        }
    }
}

#[test]
fn duplicate_keys_are_refused() {
    let raw = br#"{"v":1,"v":1,"grant_id":"x"}"#;
    let e = decode_input("shiksha.rc.input.v1", raw).unwrap_err();
    assert_eq!(e.reason(), "bad_json");
}

#[test]
fn a_float_sequence_number_is_not_an_integer() {
    let raw = br#"{"v":1,"grant_id":"00000000-0000-4000-8000-000000000001","epoch":"00000000-0000-4000-8000-000000000002","seq":1.0,"challenge":"AAAAAAAAAAAAAAAAAAAAAA","capture_generation":3,"kind":"reset","body":{"reason":"blur"}}"#;
    let e = decode_input("shiksha.rc.input.v1", raw).unwrap_err();
    assert_eq!(e.reason(), "bad_seq");
}
