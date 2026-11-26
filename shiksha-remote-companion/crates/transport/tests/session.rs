//! The network-facing layer, driven with real JSON bytes and the fake adapter.
//!
//! Not yet compiled or run (see HANDOFF, RC-012). Each refusal here has a
//! positive twin that goes through the same path.

use std::sync::Arc;

use shiksha_companion_core::adapter::FakeInputAdapter;
use shiksha_companion_core::clock::{CountingEntropy, FakeClock, Mono, MonotonicClock};
use shiksha_companion_core::companion::*;
use shiksha_companion_core::ticket::{
    ActivationClaims, KeySet, LeaseClaims, VerifiedHeader, ACTIVATION_TYP, AUDIENCE, LEASE_TYP,
};
use shiksha_companion_core::wire::{TOPIC_INPUT, TOPIC_MOTION, TOPIC_STATUS};
use shiksha_companion_transport::session::{lower, Driver, NetEvent};

const GRANT: &str = "00000000-0000-4000-8000-000000000001";
const EPOCH: &str = "00000000-0000-4000-8000-000000000002";

#[derive(Clone)]
struct Clk(Arc<FakeClock>);
impl MonotonicClock for Clk {
    fn now(&self) -> Mono {
        self.0.now()
    }
}

type D = Driver<FakeInputAdapter, Clk, CountingEntropy>;

fn hdr(typ: &str) -> VerifiedHeader {
    VerifiedHeader { alg: "EdDSA".into(), typ: typ.into(), kid: "k1".into() }
}

/// A driver whose companion is activated and holds its first lease.
fn open() -> (D, Clk) {
    let clk = Clk(Arc::new(FakeClock::new()));
    let mut co = Companion::new(
        FakeInputAdapter::new(),
        clk.clone(),
        CountingEntropy::default(),
        StopLatch::new(),
        DeviceIdentity { device_id: "dev-1".into(), key_thumbprint: "thumb".into() },
    );
    co.approve_share(InputOrigin::Physical, "room-1", "hb-1").unwrap();
    co.capture_published(Capture {
        track_sid: "TR_1".into(),
        display_id: "disp".into(),
        capture_generation: 3,
        companion_identity: "comp".into(),
        companion_sid: "PA_comp".into(),
    })
    .unwrap();
    let keys = KeySet::new(vec!["k1".into()]);
    let caps: Vec<String> = ["pointer.move", "pointer.button", "keyboard.physical"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let claims = ActivationClaims {
        aud: AUDIENCE.into(),
        protocol: 1,
        grant_id: GRANT.into(),
        grant_version: 4,
        epoch: EPOCH.into(),
        room_name: "room-1".into(),
        controller_identity: "ctrl".into(),
        controller_sid: "PA_ctrl".into(),
        companion_identity: "comp".into(),
        companion_sid: "PA_comp".into(),
        device_id: "dev-1".into(),
        device_thumbprint: "thumb".into(),
        host_binding_id: "hb-1".into(),
        capabilities: caps.clone(),
        capture_generation: 3,
        track_sid: "TR_1".into(),
        display_id: "disp".into(),
        nbf: 1_000,
        exp: 1_600,
    };
    co.activate(
        InputOrigin::Physical,
        &hdr(ACTIVATION_TYP),
        &keys,
        claims,
        "hash".into(),
        caps,
        1_000,
        clk.now(),
    )
    .unwrap();
    let req = co.begin_lease().unwrap();
    clk.0.advance(10);
    let lease = LeaseClaims {
        aud: AUDIENCE.into(),
        protocol: 1,
        grant_id: GRANT.into(),
        grant_version: 4,
        epoch: EPOCH.into(),
        device_id: "dev-1".into(),
        lease_seq: 1,
        nonce: req.nonce,
        activation_hash: "hash".into(),
        iat: 5_000,
        exp: 5_003,
    };
    co.on_lease(&hdr(LEASE_TYP), &keys, &lease).unwrap();
    (Driver::new(co), clk)
}

fn ctrl() -> Option<Sender> {
    Some(Sender { identity: "ctrl".into(), sid: "PA_ctrl".into() })
}

fn key_json(challenge: &str, seq: u64, code: &str) -> Vec<u8> {
    format!(
        r#"{{"v":1,"grant_id":"{GRANT}","epoch":"{EPOCH}","seq":{seq},"challenge":"{challenge}","capture_generation":3,"kind":"key","body":{{"code":"{code}","down":true}}}}"#
    )
    .into_bytes()
}

fn data(sender: Option<Sender>, topic: &str, payload: Vec<u8>) -> NetEvent {
    NetEvent::Data { sender, topic: topic.into(), payload }
}

fn injected(d: &D) -> usize {
    d.companion().adapter().injected().len()
}

#[test]
fn a_real_json_key_packet_from_the_controller_reaches_the_adapter() {
    let (mut d, _) = open();
    let ch = d.companion().live_challenges().last().cloned().unwrap();
    let fx = d.handle(data(ctrl(), TOPIC_INPUT, key_json(&ch, 1, "KeyA")));
    assert_eq!(injected(&d), 1, "the positive twin of every refusal below");
    assert_eq!(fx.dropped, 0);
}

#[test]
fn the_same_bytes_from_a_stranger_or_the_server_do_nothing() {
    let (mut d, _) = open();
    let ch = d.companion().live_challenges().last().cloned().unwrap();
    let stranger = Some(Sender { identity: "stranger".into(), sid: "PA_x".into() });
    assert!(d.handle(data(stranger, TOPIC_INPUT, key_json(&ch, 1, "KeyA"))).is_empty());
    assert!(d.handle(data(None, TOPIC_INPUT, key_json(&ch, 1, "KeyA"))).is_empty());
    assert_eq!(injected(&d), 0);
}

#[test]
fn other_topics_never_reach_the_core() {
    let (mut d, _) = open();
    let ch = d.companion().live_challenges().last().cloned().unwrap();
    for topic in ["shiksha.file.bytes.v1", "shiksha.rc.status.v1", "", "chat"] {
        let fx = d.handle(data(ctrl(), topic, key_json(&ch, 1, "KeyA")));
        assert!(fx.is_empty(), "{topic:?}");
    }
    assert_eq!(injected(&d), 0);
}

#[test]
fn a_reliable_kind_on_the_motion_topic_is_refused() {
    let (mut d, _) = open();
    let ch = d.companion().live_challenges().last().cloned().unwrap();
    d.handle(data(ctrl(), TOPIC_MOTION, key_json(&ch, 1, "KeyA")));
    assert_eq!(injected(&d), 0);
}

#[test]
fn undecodable_bytes_from_the_controller_get_an_addressed_error_not_silence() {
    let (mut d, _) = open();
    let fx = d.handle(data(ctrl(), TOPIC_INPUT, b"{not json".to_vec()));
    assert_eq!(fx.packets.len(), 1);
    let p = &fx.packets[0];
    assert_eq!(p.to_identity, "ctrl");
    assert_eq!(p.topic, TOPIC_STATUS);
    assert!(p.reliable());
    let body: serde_json::Value = serde_json::from_slice(&p.payload).unwrap();
    assert_eq!(body["kind"], "error");
    assert_eq!(body["body"]["code"], "bad_json");
    assert_eq!(body["grant_id"], GRANT);
}

#[test]
fn undecodable_bytes_from_a_stranger_get_no_reply() {
    let (mut d, _) = open();
    let stranger = Some(Sender { identity: "stranger".into(), sid: "PA_x".into() });
    assert!(d.handle(data(stranger, TOPIC_INPUT, b"{not json".to_vec())).is_empty());
}

#[test]
fn an_oversize_packet_is_refused_before_parsing() {
    let (mut d, _) = open();
    let fx = d.handle(data(ctrl(), TOPIC_MOTION, vec![b'x'; 1201]));
    let body: serde_json::Value = serde_json::from_slice(&fx.packets[0].payload).unwrap();
    assert_eq!(body["body"]["code"], "oversize");
}

#[test]
fn a_flood_of_garbage_from_the_controller_ends_the_grant_and_says_so() {
    let (mut d, _) = open();
    let mut stopped = false;
    for _ in 0..20 {
        let fx = d.handle(data(ctrl(), TOPIC_INPUT, b"garbage".to_vec()));
        for p in &fx.packets {
            let body: serde_json::Value = serde_json::from_slice(&p.payload).unwrap();
            if body["kind"] == "stopped" {
                assert_eq!(body["body"]["reason"], "protocol_violation");
                stopped = true;
            }
        }
        if stopped {
            assert_eq!(fx.revokes.len(), 1);
            break;
        }
    }
    assert!(stopped);
    assert!(!d.companion().is_controlled());
}

#[test]
fn a_transport_disconnect_stops_the_grant_and_addresses_the_notice_to_the_controller() {
    let (mut d, _) = open();
    let fx = d.handle(NetEvent::Transport(TransportEvent::Reconnected));
    assert!(!d.companion().is_controlled());
    assert!(fx.packets.iter().all(|p| p.to_identity == "ctrl" && p.topic == TOPIC_STATUS));
    assert_eq!(fx.revokes.len(), 1);
}

#[test]
fn tick_emits_challenges_addressed_to_the_controller() {
    let (mut d, clk) = open();
    clk.0.advance(250);
    let fx = d.tick();
    assert!(!fx.packets.is_empty());
    for p in &fx.packets {
        assert_eq!(p.to_identity, "ctrl");
        let body: serde_json::Value = serde_json::from_slice(&p.payload).unwrap();
        assert!(matches!(body["kind"].as_str(), Some("challenge") | Some("ack")));
    }
}

#[test]
fn a_status_with_no_destination_is_dropped_and_counted_never_broadcast() {
    let fx = lower(vec![Outbound::Status {
        to_identity: String::new(),
        grant_id: GRANT.into(),
        epoch: EPOCH.into(),
        status: Status::Error { code: "x" },
    }]);
    assert!(fx.packets.is_empty());
    assert_eq!(fx.dropped, 1);
}

#[test]
fn status_bodies_carry_no_input_content() {
    let (mut d, _) = open();
    let ch = d.companion().live_challenges().last().cloned().unwrap();
    // Inject a recognisable key, then force every kind of status out (a refused
    // packet, a stop). Nothing about the key may appear in any of them.
    let mut fx = d.handle(data(ctrl(), TOPIC_INPUT, key_json(&ch, 1, "KeyQ")));
    assert_eq!(injected(&d), 1, "the key really was handled");
    fx.extend(d.handle(data(ctrl(), TOPIC_INPUT, b"{bad".to_vec())));
    fx.extend(d.handle(NetEvent::Transport(TransportEvent::Disconnected)));
    assert!(!fx.packets.is_empty());
    for p in &fx.packets {
        let s = String::from_utf8_lossy(&p.payload);
        assert!(!s.contains("KeyQ"), "no key identity in status: {s}");
    }
}
