//! The real Windows adapter, driven only through the core's real gate.
//!
//! No test-support shortcut is used here: a `ValidatedEvent` can only come from
//! `Companion::pump`, which is exactly the property under test. The OS is replaced
//! by a recording sink; nothing is actually injected.
//!
//! Not run on this machine (blocked executables); type-checked only.

use std::sync::{Arc, Mutex};

use shiksha_companion_core::clock::{CountingEntropy, FakeClock, Mono, MonotonicClock};
use shiksha_companion_core::companion::*;
use shiksha_companion_core::ticket::{
    ActivationClaims, KeySet, LeaseClaims, VerifiedHeader, ACTIVATION_TYP, AUDIENCE, LEASE_TYP,
};
use shiksha_companion_core::wire::{Body, Envelope};
use shiksha_companion_platform_windows::display::{Bounds, DisplayDescriptor};
use shiksha_companion_platform_windows::input::*;
use shiksha_companion_platform_windows::provenance::{PhysicalState, LLKHF_INJECTED};

const GRANT: &str = "00000000-0000-4000-8000-000000000001";
const EPOCH: &str = "00000000-0000-4000-8000-000000000002";

/// (type, flags, scan) of everything "sent", one entry per input.
type Sent = Vec<(u32, u32, u16)>;

#[derive(Clone, Default)]
struct RecSink {
    sent: Arc<Mutex<Sent>>,
    calls: Arc<Mutex<usize>>,
    fail: Arc<Mutex<Option<SendError>>>,
}

impl InputSink for RecSink {
    fn send(&mut self, inputs: &[Input]) -> Result<(), SendError> {
        if let Some(e) = *self.fail.lock().unwrap() {
            return Err(e);
        }
        *self.calls.lock().unwrap() += 1;
        let mut s = self.sent.lock().unwrap();
        for i in inputs {
            // SAFETY: reading the union member selected by `type_`.
            unsafe {
                if i.type_ == INPUT_MOUSE {
                    s.push((INPUT_MOUSE, i.u.mi.dw_flags, 0));
                } else {
                    s.push((INPUT_KEYBOARD, i.u.ki.dw_flags, i.u.ki.w_scan));
                }
            }
        }
        Ok(())
    }
    fn cursor(&self) -> Option<(i32, i32)> {
        None // not verifying position in these tests
    }
}

#[derive(Clone)]
struct Clk(Arc<FakeClock>);
impl MonotonicClock for Clk {
    fn now(&self) -> Mono {
        self.0.now()
    }
}

type Co = Companion<WindowsInput<RecSink, Arc<PhysicalState>>, Clk, CountingEntropy>;

struct Rig {
    co: Co,
    sink: RecSink,
    phys: Arc<PhysicalState>,
    seq: u64,
}

fn hdr(t: &str) -> VerifiedHeader {
    VerifiedHeader { alg: "EdDSA".into(), typ: t.into(), kid: "k1".into() }
}

fn rig(open: bool) -> Rig {
    let clk = Clk(Arc::new(FakeClock::new()));
    let sink = RecSink::default();
    let phys = Arc::new(PhysicalState::new());
    let display = DisplayDescriptor {
        display_id: "d".into(),
        pixel_width: 1920,
        pixel_height: 1080,
        rotation: 0,
        bounds: Bounds { x: 0, y: 0, width: 1920, height: 1080 },
        dpi_scale: 1.0,
        primary: true,
    };
    let adapter = WindowsInput::new(sink.clone(), phys.clone(), display.clone(), display.bounds);
    let mut co = Companion::new(
        adapter,
        clk.clone(),
        CountingEntropy::default(),
        StopLatch::new(),
        DeviceIdentity { device_id: "dev".into(), key_thumbprint: "tp".into() },
    );
    co.approve_share(InputOrigin::Physical, "room", "hb").unwrap();
    co.capture_published(Capture {
        track_sid: "TR".into(),
        display_id: "d".into(),
        capture_generation: 1,
        companion_identity: "comp".into(),
        companion_sid: "PA_comp".into(),
    })
    .unwrap();
    if open {
        let caps: Vec<String> = ["pointer.move", "pointer.button", "keyboard.physical", "keyboard.text"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let keys = KeySet::new(vec!["k1".into()]);
        let claims = ActivationClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: GRANT.into(),
            grant_version: 1,
            epoch: EPOCH.into(),
            room_name: "room".into(),
            controller_identity: "ctrl".into(),
            controller_sid: "PA_ctrl".into(),
            companion_identity: "comp".into(),
            companion_sid: "PA_comp".into(),
            device_id: "dev".into(),
            device_thumbprint: "tp".into(),
            host_binding_id: "hb".into(),
            capabilities: caps.clone(),
            capture_generation: 1,
            track_sid: "TR".into(),
            display_id: "d".into(),
            nbf: 1_000,
            exp: 1_600,
        };
        co.activate(InputOrigin::Physical, &hdr(ACTIVATION_TYP), &keys, claims, "h".into(), caps, 1_000, clk.now())
            .unwrap();
        let req = co.begin_lease().unwrap();
        clk.0.advance(10);
        let lease = LeaseClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: GRANT.into(),
            grant_version: 1,
            epoch: EPOCH.into(),
            device_id: "dev".into(),
            lease_seq: 1,
            nonce: req.nonce,
            activation_hash: "h".into(),
            iat: 5_000,
            exp: 5_003,
        };
        co.on_lease(&hdr(LEASE_TYP), &keys, &lease).unwrap();
    }
    Rig { co, sink, phys, seq: 0 }
}

impl Rig {
    fn send(&mut self, body: Body) {
        self.seq += 1;
        let challenge = self
            .co
            .live_challenges()
            .last()
            .cloned()
            .unwrap_or_else(|| "AAAAAAAAAAAAAAAAAAAAAA".into());
        let topic = if body.is_motion() { Topic::Motion } else { Topic::Input };
        let envelope = Envelope {
            grant_id: GRANT.into(),
            epoch: EPOCH.into(),
            seq: self.seq,
            challenge,
            capture_generation: 1,
            body,
        };
        let sender = Some(Sender { identity: "ctrl".into(), sid: "PA_ctrl".into() });
        self.co.on_packet(Inbound { sender, topic, envelope });
        self.co.pump();
    }
    fn calls(&self) -> usize {
        *self.sink.calls.lock().unwrap()
    }
}

fn key(code: &str, down: bool) -> Body {
    Body::Key { code: code.into(), down }
}

#[test]
fn with_the_gate_open_a_key_reaches_sendinput_as_a_scan_code() {
    // The positive twin of every "zero calls" test below.
    let mut r = rig(true);
    r.send(key("KeyA", true));
    assert_eq!(r.calls(), 1);
    assert_eq!(r.sink.sent.lock().unwrap()[0], (INPUT_KEYBOARD, KEYEVENTF_SCANCODE, 0x1E));
}

#[test]
fn with_no_grant_the_same_packet_makes_zero_sendinput_calls() {
    let mut r = rig(false);
    r.send(key("KeyA", true));
    r.send(Body::Move { u: 0.5, v: 0.5 });
    assert_eq!(r.calls(), 0);
}

#[test]
fn a_blocked_desktop_ends_the_grant() {
    // UAC prompt / lock screen: SendInput is refused with access denied.
    let mut r = rig(true);
    *r.sink.fail.lock().unwrap() = Some(SendError::Blocked);
    r.send(key("KeyA", true));
    assert!(!r.co.is_controlled(), "an adapter failure ends the epoch");
}

#[test]
fn physical_activity_ends_control_and_releases_a_remote_key_through_sendinput() {
    let mut r = rig(true);
    r.send(key("KeyA", true));
    let before = r.calls();
    r.co.on_transport(TransportEvent::PhysicalActivity);
    assert!(!r.co.is_controlled());
    assert_eq!(r.calls(), before + 1, "exactly the key-up");
    let last = *r.sink.sent.lock().unwrap().last().unwrap();
    assert_eq!(last, (INPUT_KEYBOARD, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0x1E));
}

#[test]
fn ending_control_does_not_release_a_shift_the_owner_is_physically_holding() {
    let mut r = rig(true);
    r.send(key("ShiftLeft", true));
    r.send(key("KeyA", true));
    // The owner is really holding Shift (a non-injected event, scan 0x2A).
    r.phys.on_key(0x2A, 0);
    r.co.local_stop();
    let sent = r.sink.sent.lock().unwrap().clone();
    let ups: Vec<u16> = sent
        .iter()
        .filter(|(t, f, _)| *t == INPUT_KEYBOARD && f & KEYEVENTF_KEYUP != 0)
        .map(|(_, _, s)| *s)
        .collect();
    assert_eq!(ups, vec![0x1E], "A is released; the owner's Shift (0x2A) is theirs");
}

#[test]
fn a_remote_shift_is_released_when_the_owner_is_not_holding_one() {
    // Control for the test above: same flow, no physical Shift.
    let mut r = rig(true);
    r.send(key("ShiftLeft", true));
    r.co.local_stop();
    let sent = r.sink.sent.lock().unwrap().clone();
    assert!(sent
        .iter()
        .any(|(t, f, s)| *t == INPUT_KEYBOARD && f & KEYEVENTF_KEYUP != 0 && *s == 0x2A));
}

#[test]
fn injected_events_never_register_as_the_owner_even_with_the_real_adapter_wired_in() {
    let r = rig(true);
    r.phys.on_key(0x2A, LLKHF_INJECTED);
    assert!(!r.phys.take_activity());
    assert!(!r.phys.modifier_held("ShiftLeft"));
}
