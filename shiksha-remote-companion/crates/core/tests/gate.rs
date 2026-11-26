//! Observable authorisation behaviour of [`Companion`], driven only through its
//! public API with a fake clock and a fake OS adapter.
//!
//! The rule for this file (HANDOFF §8): a test that cannot fail is worse than
//! none. Every refusal test is paired with a success test that goes through the
//! same path, and every test that asserts "zero OS calls" first proves that the
//! same harness *does* produce calls when the gate is open.
//!
//! Maps to security-test-plan T04, T05, T06, T08, T10, T13, T14, T20.

use std::sync::Arc;

use shiksha_companion_core::adapter::{Action, Call, FakeInputAdapter, Release};
use shiksha_companion_core::clock::{CountingEntropy, FakeClock, Mono, MonotonicClock};
use shiksha_companion_core::companion::*;
use shiksha_companion_core::ticket::{
    ActivationClaims, KeySet, LeaseClaims, VerifiedHeader, ACTIVATION_TYP, AUDIENCE, LEASE_TYP,
};
use shiksha_companion_core::wire::{Body, Button, Envelope, ResetReason};

const GRANT: &str = "00000000-0000-4000-8000-000000000001";
const EPOCH: &str = "00000000-0000-4000-8000-000000000002";

/// Clock handle the test keeps while the companion owns a clone.
#[derive(Clone)]
struct SharedClock(Arc<FakeClock>);
impl MonotonicClock for SharedClock {
    fn now(&self) -> Mono {
        self.0.now()
    }
}

type Co = Companion<FakeInputAdapter, SharedClock, CountingEntropy>;

struct Rig {
    co: Co,
    clock: SharedClock,
    keys: KeySet,
    lease_seq: u64,
    seq: u64,
    motion_seq: u64,
}

fn header(typ: &str) -> VerifiedHeader {
    VerifiedHeader { alg: "EdDSA".into(), typ: typ.into(), kid: "k1".into() }
}

fn claims(caps: &[&str]) -> ActivationClaims {
    ActivationClaims {
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
        capabilities: caps.iter().map(|s| s.to_string()).collect(),
        capture_generation: 3,
        track_sid: "TR_1".into(),
        display_id: "disp".into(),
        nbf: 1_000,
        exp: 1_600,
    }
}

const ALL_CAPS: [&str; 5] =
    ["pointer.move", "pointer.button", "pointer.scroll", "keyboard.physical", "keyboard.text"];

impl Rig {
    /// Companion that has approved sharing and published a capture, but has not
    /// been activated.
    fn shared() -> Rig {
        let clock = SharedClock(Arc::new(FakeClock::new()));
        let mut co = Companion::new(
            FakeInputAdapter::new(),
            clock.clone(),
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
        Rig {
            co,
            clock,
            keys: KeySet::new(vec!["k1".into()]),
            lease_seq: 0,
            seq: 0,
            motion_seq: 0,
        }
    }

    fn activate_with(&mut self, caps: &[&str]) -> Result<(), Refusal> {
        let now = self.clock.now();
        self.co.activate(
            InputOrigin::Physical,
            &header(ACTIVATION_TYP),
            &self.keys,
            claims(caps),
            "hash".into(),
            ALL_CAPS.iter().map(|s| s.to_string()).collect(),
            1_000,
            now,
        )
    }

    /// Activated and holding its first lease: the gate is open.
    fn open(caps: &[&str]) -> Rig {
        let mut r = Rig::shared();
        r.activate_with(caps).unwrap();
        r.renew();
        r
    }

    fn open_all() -> Rig {
        Rig::open(&ALL_CAPS)
    }

    fn lease_reply(&mut self, nonce: &str) -> LeaseClaims {
        self.lease_seq += 1;
        LeaseClaims {
            aud: AUDIENCE.into(),
            protocol: 1,
            grant_id: GRANT.into(),
            grant_version: 4,
            epoch: EPOCH.into(),
            device_id: "dev-1".into(),
            lease_seq: self.lease_seq,
            nonce: nonce.into(),
            activation_hash: "hash".into(),
            iat: 5_000,
            exp: 5_003,
        }
    }

    /// One successful renewal round trip taking `rtt_ms`.
    fn renew_after(&mut self, rtt_ms: u64) -> Vec<Outbound> {
        let req = self.co.begin_lease().expect("a grant is active");
        self.clock.0.advance(rtt_ms);
        let reply = self.lease_reply(&req.nonce);
        self.co.on_lease(&header(LEASE_TYPE), &self.keys, &reply).expect("lease accepted")
    }

    fn renew(&mut self) -> Vec<Outbound> {
        self.renew_after(10)
    }

    fn challenge(&self) -> String {
        // After authority ends there is no live challenge; a controller would
        // keep echoing its last one, so any well-formed value stands in.
        self.co
            .live_challenges()
            .last()
            .cloned()
            .unwrap_or_else(|| "AAAAAAAAAAAAAAAAAAAAAA".to_string())
    }

    fn env(&mut self, body: Body) -> Envelope {
        let motion = body.is_motion();
        let seq = if motion {
            self.motion_seq += 1;
            self.motion_seq
        } else {
            self.seq += 1;
            self.seq
        };
        Envelope {
            grant_id: GRANT.into(),
            epoch: EPOCH.into(),
            seq,
            challenge: self.challenge(),
            capture_generation: 3,
            body,
        }
    }

    fn from_controller(&self, topic: Topic, envelope: Envelope) -> Inbound {
        Inbound {
            sender: Some(Sender { identity: "ctrl".into(), sid: "PA_ctrl".into() }),
            topic,
            envelope,
        }
    }

    fn send(&mut self, body: Body) -> Vec<Outbound> {
        let topic = if body.is_motion() { Topic::Motion } else { Topic::Input };
        let e = self.env(body);
        let p = self.from_controller(topic, e);
        let mut out = self.co.on_packet(p);
        out.extend(self.co.pump());
        out
    }

    /// A controller heartbeat holding exactly `held`.
    fn beat(&mut self, held: &[&str]) {
        self.send(Body::Heartbeat {
            keys: held.iter().map(|k| k.to_string()).collect(),
            buttons: vec![],
            last_ack: 0,
        });
    }

    /// Let `ms` pass in 250 ms steps with a live controller (a heartbeat each
    /// step) but **no lease renewals**. Returns everything the ticks emitted.
    fn run(&mut self, ms: u64) -> Vec<Outbound> {
        let mut out = Vec::new();
        let mut left = ms;
        while left > 0 {
            self.beat(&[]);
            let step = left.min(250);
            self.clock.0.advance(step);
            left -= step;
            out.extend(self.co.tick());
        }
        out
    }

    /// As `run`, but also renewing the lease about once a second and holding
    /// `held` in every heartbeat: a healthy, long-lived session.
    fn run_healthy(&mut self, ms: u64, held: &[&str]) {
        let mut left = ms;
        let mut since_renew = 0;
        while left > 0 {
            self.beat(held);
            let step = left.min(250);
            self.clock.0.advance(step);
            left -= step;
            since_renew += step;
            self.co.tick();
            if since_renew >= 1_000 {
                self.renew_after(1);
                since_renew = 0;
            }
        }
    }

    fn calls(&self) -> &Vec<Call> {
        &self.co.adapter().calls
    }
}

// The activation header constant is named differently from the lease one in the
// crate; alias it for readability.
const LEASE_TYPE: &str = LEASE_TYP;

fn key(code: &str, down: bool) -> Body {
    Body::Key { code: code.into(), down }
}
fn click(down: bool) -> Body {
    Body::Button { button: Button::Left, down, u: 0.5, v: 0.5, motion_seq: 0 }
}

fn stopped(out: &[Outbound]) -> Option<&'static str> {
    out.iter().find_map(|o| match o {
        Outbound::Status { status: Status::Stopped { reason }, .. } => Some(*reason),
        _ => None,
    })
}

// ── Positive paths first ──────────────────────────────────────────────────

#[test]
fn an_open_gate_injects_what_the_controller_sends() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    r.send(key("KeyA", false));
    r.send(click(true));
    r.send(click(false));
    assert_eq!(
        r.co.adapter().injected().len(),
        4,
        "the harness really does reach the adapter when everything is valid"
    );
}

#[test]
fn first_lease_opens_the_gate_and_announces_ready_with_a_challenge() {
    let mut r = Rig::shared();
    r.activate_with(&ALL_CAPS).unwrap();
    let out = r.renew();
    assert!(out.iter().any(|o| matches!(
        o,
        Outbound::Status { status: Status::Ready { capture_generation: 3, .. }, to_identity, .. }
            if to_identity == "ctrl"
    )));
    assert!(out
        .iter()
        .any(|o| matches!(o, Outbound::Status { status: Status::Challenge { valid_for_ms: 1000, .. }, .. })));
}

// ── Consent ───────────────────────────────────────────────────────────────

#[test]
fn injected_input_cannot_grant_consent_or_share_approval() {
    // T13: the owner's dialogs must not be answerable by remote events.
    let mut r = Rig::shared();
    let now = r.clock.now();
    let res = r.co.activate(
        InputOrigin::Injected,
        &header(ACTIVATION_TYP),
        &r.keys,
        claims(&ALL_CAPS),
        "hash".into(),
        ALL_CAPS.iter().map(|s| s.to_string()).collect(),
        1_000,
        now,
    );
    assert_eq!(res, Err(Refusal::NotPhysical));
    assert!(!r.co.is_controlled());

    let mut idle = Companion::new(
        FakeInputAdapter::new(),
        SharedClock(Arc::new(FakeClock::new())),
        CountingEntropy::default(),
        StopLatch::new(),
        DeviceIdentity { device_id: "d".into(), key_thumbprint: "t".into() },
    );
    assert_eq!(
        idle.approve_share(InputOrigin::Injected, "room-1", "hb-1"),
        Err(Refusal::NotPhysical)
    );
    // And the physical path through the same call succeeds.
    assert!(idle.approve_share(InputOrigin::Physical, "room-1", "hb-1").is_ok());
}

#[test]
fn never_joins_at_idle_startup_nor_an_unapproved_room() {
    let idle = Companion::new(
        FakeInputAdapter::new(),
        SharedClock(Arc::new(FakeClock::new())),
        CountingEntropy::default(),
        StopLatch::new(),
        DeviceIdentity { device_id: "d".into(), key_thumbprint: "t".into() },
    );
    assert!(!idle.may_join("room-1", "hb-1"), "idle companion joins nothing");

    let r = Rig::shared();
    assert!(r.co.may_join("room-1", "hb-1"));
    assert!(!r.co.may_join("room-2", "hb-1"), "wrong room");
    assert!(!r.co.may_join("room-1", "hb-other"), "wrong binding");
}

#[test]
fn approving_sharing_is_not_consent_to_control() {
    let mut r = Rig::shared();
    r.co.on_packet(r.from_controller(Topic::Input, Envelope {
        grant_id: GRANT.into(),
        epoch: EPOCH.into(),
        seq: 1,
        challenge: "AAAAAAAAAAAAAAAAAAAAAA".into(),
        capture_generation: 3,
        body: key("KeyA", true),
    }));
    r.co.pump();
    assert!(r.calls().is_empty(), "a share approval alone injects nothing");
}

#[test]
fn a_second_activation_is_refused_while_one_can_inject() {
    let mut r = Rig::open_all();
    assert_eq!(r.activate_with(&ALL_CAPS), Err(Refusal::GrantInProgress));
}

#[test]
fn ticket_for_a_different_capture_or_room_is_refused() {
    let mut r = Rig::shared();
    let mut c = claims(&ALL_CAPS);
    c.capture_generation = 9;
    let now = r.clock.now();
    let res = r.co.activate(
        InputOrigin::Physical,
        &header(ACTIVATION_TYP),
        &r.keys,
        c,
        "hash".into(),
        ALL_CAPS.iter().map(|s| s.to_string()).collect(),
        1_000,
        now,
    );
    assert!(matches!(res, Err(Refusal::Ticket(_))));
    assert!(!r.co.is_controlled());
}

#[test]
fn activation_with_a_wrong_algorithm_or_unknown_key_is_refused() {
    for (alg, kid) in [("HS256", "k1"), ("none", "k1"), ("EdDSA", "unknown")] {
        let mut r = Rig::shared();
        let now = r.clock.now();
        let h = VerifiedHeader { alg: alg.into(), typ: ACTIVATION_TYP.into(), kid: kid.into() };
        let res = r.co.activate(
            InputOrigin::Physical,
            &h,
            &r.keys,
            claims(&ALL_CAPS),
            "hash".into(),
            ALL_CAPS.iter().map(|s| s.to_string()).collect(),
            1_000,
            now,
        );
        assert!(matches!(res, Err(Refusal::Ticket(_))), "{alg}/{kid}");
    }
}

// ── A ticket is not permission: the lease is ──────────────────────────────

#[test]
fn a_valid_ticket_with_no_lease_injects_nothing() {
    let mut r = Rig::shared();
    r.activate_with(&ALL_CAPS).unwrap();
    // No lease yet, so no challenge exists to echo; send with a made-up one.
    let p = r.from_controller(Topic::Input, Envelope {
        grant_id: GRANT.into(),
        epoch: EPOCH.into(),
        seq: 1,
        challenge: "AAAAAAAAAAAAAAAAAAAAAA".into(),
        capture_generation: 3,
        body: key("KeyA", true),
    });
    r.co.on_packet(p);
    r.co.pump();
    assert!(r.calls().is_empty());
    assert!(r.co.is_controlled(), "still waiting for the first lease, not torn down");
}

#[test]
fn a_grant_that_never_gets_its_first_lease_is_abandoned() {
    let mut r = Rig::shared();
    r.activate_with(&ALL_CAPS).unwrap();
    r.clock.0.advance(3_001);
    let out = r.co.tick();
    assert_eq!(stopped(&out), Some("stale_lease"));
    assert!(!r.co.is_controlled());
}

#[test]
fn a_slow_first_lease_does_not_eat_the_controllers_first_heartbeat_window() {
    // Regression: the heartbeat deadline used to be set at activation. The first
    // lease may legitimately take up to 1 s, after which the controller (which
    // only starts on `ready`) had almost no time left.
    let mut r = Rig::shared();
    r.activate_with(&ALL_CAPS).unwrap();
    r.renew_after(900); // ready at t=900
    r.clock.0.advance(500); // controller sends its first heartbeat at t=1400
    let out = r.send(Body::Heartbeat { keys: vec![], buttons: vec![], last_ack: 0 });
    assert!(stopped(&out).is_none(), "first heartbeat inside its own 1 s window");
    assert!(r.co.is_controlled());
}

// ── T08: revocation is bounded by the lease ───────────────────────────────

#[test]
fn input_stops_when_the_lease_lapses_and_nothing_renews() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    assert_eq!(r.co.adapter().injected().len(), 1);

    // Server revoked: no renewal arrives. 3 s from request start (t=0).
    r.clock.0.advance(3_000);
    let out = r.co.tick();
    assert_eq!(stopped(&out), Some("stale_lease"));
    assert!(!r.co.is_controlled());
    assert!(
        r.co.adapter().releases().contains(&&Release::Key("KeyA".into())),
        "the held key is released when authority ends"
    );
}

#[test]
fn a_late_lease_reply_is_discarded_and_the_old_deadline_stands() {
    let mut r = Rig::open_all();
    let req = r.co.begin_lease().unwrap();
    r.clock.0.advance(1_500); // reply takes 1.5 s: over the 1 s cap
    let reply = r.lease_reply(&req.nonce);
    assert!(r.co.on_lease(&header(LEASE_TYPE), &r.keys, &reply).is_err());
    // First lease was requested at t=0 and expires at t=3000.
    r.clock.0.advance(1_500);
    let out = r.co.tick();
    assert_eq!(stopped(&out), Some("stale_lease"), "late reply bought no time");
}

#[test]
fn lease_deadline_runs_from_request_start_so_a_slow_reply_shortens_it() {
    let mut r = Rig::open_all();
    // This renewal starts at t=10 and its reply lands at t=910. The deadline is
    // 10 + 3000 = 3010, not 910 + 3000 = 3910.
    r.renew_after(900);
    let early = r.run(2_099); // to t=3009
    assert!(stopped(&early).is_none(), "still inside the window");
    r.clock.0.advance(2); // t=3011
    assert_eq!(stopped(&r.co.tick()), Some("stale_lease"));
}

// ── Lease-stall check at the injection site ───────────────────────────────

#[test]
fn an_event_queued_before_a_stall_is_not_injected_after_it() {
    let mut r = Rig::open_all();
    let e = r.env(key("KeyA", true));
    let p = r.from_controller(Topic::Input, e);
    r.co.on_packet(p); // queued, not pumped
    r.clock.0.advance(5_000); // OS scheduler stall: lease and challenge both lapse
    let out = r.co.pump();
    assert!(r.co.adapter().injected().is_empty(), "checked at injection time");
    assert_eq!(stopped(&out), Some("stale_lease"));
}

// ── Local Stop ────────────────────────────────────────────────────────────

#[test]
fn local_stop_closes_the_gate_releases_and_only_then_acknowledges() {
    let mut r = Rig::open_all();
    r.send(key("ShiftLeft", true));
    r.send(click(true));
    let out = r.co.local_stop();

    // Release calls happen, and the Stopped status is produced after them.
    assert_eq!(stopped(&out), Some("local_stop"));
    let rel = r.co.adapter().releases();
    assert!(rel.contains(&&Release::Key("ShiftLeft".into())));
    assert!(rel.contains(&&Release::Button(Button::Left)));

    // Nothing the controller sends afterwards does anything.
    let before = r.calls().len();
    r.send(key("KeyA", true));
    assert_eq!(r.calls().len(), before);
}

#[test]
fn the_ui_thread_can_close_the_gate_without_taking_the_companion() {
    let mut r = Rig::open_all();
    let latch = r.co.stop_handle();
    r.send(key("KeyA", true));
    assert_eq!(r.co.adapter().injected().len(), 1);

    // An event is already queued when Stop lands on another thread.
    let e = r.env(key("KeyB", true));
    r.co.on_packet(r.from_controller(Topic::Input, e));
    latch.stop(); // lock-free; the companion is not touched
    r.co.pump();
    assert_eq!(
        r.co.adapter().injected().len(),
        1,
        "the latch alone blocks the next OS call"
    );
}

#[test]
fn cleanup_after_a_lock_free_stop_releases_and_acknowledges() {
    let mut r = Rig::open_all();
    r.send(key("ShiftLeft", true));
    r.co.stop_handle().stop();
    let out = r.co.sync_after_latch_stop();
    assert_eq!(stopped(&out), Some("local_stop"));
    assert!(r.co.adapter().releases().contains(&&Release::Key("ShiftLeft".into())));
    assert!(r.co.sync_after_latch_stop().is_empty(), "idempotent");
}

#[test]
fn a_lease_reply_that_crosses_a_local_stop_is_discarded() {
    let mut r = Rig::open_all();
    let req = r.co.begin_lease().unwrap();
    r.co.stop_handle().stop();
    let reply = r.lease_reply(&req.nonce);
    assert!(r.co.on_lease(&header(LEASE_TYPE), &r.keys, &reply).is_err());
}

#[test]
fn a_stop_is_permanent_for_the_epoch_and_a_new_activation_is_a_new_consent() {
    let mut r = Rig::open_all();
    r.co.local_stop();
    assert!(!r.co.is_controlled());
    // Reactivating needs physical consent again, and works when given.
    assert_eq!(
        {
            let now = r.clock.now();
            r.co.activate(
                InputOrigin::Injected,
                &header(ACTIVATION_TYP),
                &r.keys,
                claims(&ALL_CAPS),
                "hash".into(),
                ALL_CAPS.iter().map(|s| s.to_string()).collect(),
                1_000,
                now,
            )
        },
        Err(Refusal::NotPhysical)
    );
    assert!(r.activate_with(&ALL_CAPS).is_ok());
}

// ── T04 / T06: who may send ───────────────────────────────────────────────

#[test]
fn a_stranger_copying_the_grant_id_has_no_effect() {
    // T04. Proves the harness works first (see the first test), then that the
    // same packet from anyone else does nothing.
    let mut r = Rig::open_all();
    let e = r.env(key("KeyA", true));
    let mut p = r.from_controller(Topic::Input, e);
    p.sender = Some(Sender { identity: "stranger".into(), sid: "PA_x".into() });
    r.co.on_packet(p);
    r.co.pump();
    assert!(r.calls().is_empty());
    assert!(r.co.is_controlled(), "a stranger cannot even disturb the grant");
}

#[test]
fn server_origin_data_is_never_input() {
    let mut r = Rig::open_all();
    let e = r.env(key("KeyA", true));
    let mut p = r.from_controller(Topic::Input, e);
    p.sender = None;
    r.co.on_packet(p);
    r.co.pump();
    assert!(r.calls().is_empty());
}

#[test]
fn right_identity_wrong_sid_is_ignored_and_a_sid_change_ends_the_grant() {
    let mut r = Rig::open_all();
    let e = r.env(key("KeyA", true));
    let mut p = r.from_controller(Topic::Input, e);
    p.sender = Some(Sender { identity: "ctrl".into(), sid: "PA_old".into() });
    r.co.on_packet(p);
    r.co.pump();
    assert!(r.calls().is_empty());

    let out = r.co.on_transport(TransportEvent::ParticipantSid {
        identity: "ctrl".into(),
        sid: "PA_new".into(),
    });
    assert_eq!(stopped(&out), Some("sid_changed"));
}

#[test]
fn packets_under_another_grant_or_epoch_or_capture_do_nothing() {
    // T05/T06: replay under a new epoch / other grant / other capture generation.
    for mutate in [
        (|e: &mut Envelope| e.epoch = "00000000-0000-4000-8000-0000000000ff".into()) as fn(&mut Envelope),
        |e| e.grant_id = "00000000-0000-4000-8000-0000000000ff".into(),
        |e| e.capture_generation = 4,
    ] {
        let mut r = Rig::open_all();
        let mut e = r.env(key("KeyA", true));
        mutate(&mut e);
        let p = r.from_controller(Topic::Input, e);
        r.co.on_packet(p);
        r.co.pump();
        assert!(r.calls().is_empty());
    }
}

// ── Freshness ─────────────────────────────────────────────────────────────

#[test]
fn a_packet_must_echo_a_live_challenge() {
    let mut r = Rig::open_all();
    let mut e = r.env(key("KeyA", true));
    e.challenge = "BBBBBBBBBBBBBBBBBBBBBB".into(); // well-formed, never issued
    let p = r.from_controller(Topic::Input, e);
    r.co.on_packet(p);
    r.co.pump();
    assert!(r.calls().is_empty());
}

#[test]
fn an_old_challenge_is_refused_after_one_second() {
    let mut r = Rig::open_all();
    let stale = r.challenge();
    r.run(1_100); // the original challenge has now lapsed; fresh ones exist
    assert!(!r.co.live_challenges().contains(&stale));

    // Control: an input echoing a live challenge is injected.
    r.send(key("KeyA", true));
    assert_eq!(r.co.adapter().injected().len(), 1);

    // The same input echoing the lapsed one is not.
    let mut e = r.env(key("KeyB", true));
    e.challenge = stale;
    r.co.on_packet(r.from_controller(Topic::Input, e));
    r.co.pump();
    assert_eq!(r.co.adapter().injected().len(), 1);
}

#[test]
fn up_to_four_challenges_are_live_at_once() {
    let mut r = Rig::open_all();
    r.run(900);
    assert_eq!(r.co.live_challenges().len(), CHALLENGES_RETAINED);
}

// ── Heartbeat ─────────────────────────────────────────────────────────────

#[test]
fn no_controller_heartbeat_for_a_second_pauses_and_releases() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    r.clock.0.advance(1_000);
    r.renew();
    let out = r.co.tick();
    assert_eq!(stopped(&out), Some("heartbeat_timeout"));
    assert!(r.co.adapter().releases().contains(&&Release::Key("KeyA".into())));
}

#[test]
fn a_heartbeat_keeps_the_grant_alive_and_releases_what_the_controller_no_longer_holds() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    r.send(key("KeyB", true));
    r.clock.0.advance(500);
    r.send(Body::Heartbeat { keys: vec!["KeyA".into()], buttons: vec![], last_ack: 0 });
    let rel = r.co.adapter().releases();
    assert!(rel.contains(&&Release::Key("KeyB".into())), "B is gone at the controller");
    assert!(!rel.contains(&&Release::Key("KeyA".into())), "A is still held");
    assert!(r.co.is_controlled());
}

#[test]
fn a_heartbeat_never_synthesises_a_key_down() {
    let mut r = Rig::open_all();
    r.send(Body::Heartbeat { keys: vec!["KeyA".into()], buttons: vec![Button::Left], last_ack: 0 });
    assert!(r.co.adapter().injected().is_empty());
}

// ── T20: lost / reordered events ──────────────────────────────────────────

#[test]
fn a_gap_in_the_reliable_sequence_ends_control_and_applies_nothing_later() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    r.seq += 1; // a packet is lost in transit
    let out = r.send(key("KeyA", false));
    assert_eq!(stopped(&out), Some("sequence_gap"));
    assert_eq!(r.co.adapter().injected().len(), 1, "the key-up after the gap never ran");
    assert!(r.co.adapter().releases().contains(&&Release::Key("KeyA".into())), "and A is released");
}

#[test]
fn a_duplicate_reliable_packet_is_not_reinjected() {
    let mut r = Rig::open_all();
    let e = r.env(key("KeyA", true));
    let p = r.from_controller(Topic::Input, e.clone());
    r.co.on_packet(p);
    r.co.pump();
    let p2 = r.from_controller(Topic::Input, e);
    r.co.on_packet(p2);
    r.co.pump();
    assert_eq!(r.co.adapter().injected().len(), 1);
    assert!(r.co.is_controlled(), "a duplicate is harmless, not a violation");
}

#[test]
fn a_late_lossy_move_cannot_drag_the_cursor_behind_an_applied_click() {
    let mut r = Rig::open_all();
    // Click references motion_seq 10: the floor becomes 10.
    r.send(Body::Button { button: Button::Left, down: true, u: 0.9, v: 0.9, motion_seq: 10 });
    r.motion_seq = 4; // a move numbered 5 arrives late
    r.send(Body::Move { u: 0.1, v: 0.1 });
    let moves: Vec<_> = r
        .co
        .adapter()
        .injected()
        .into_iter()
        .filter(|a| matches!(a, Action::MoveTo { .. }))
        .collect();
    assert!(moves.is_empty(), "the stale move was dropped");
    // A move above the floor does apply.
    r.motion_seq = 10;
    r.send(Body::Move { u: 0.2, v: 0.2 });
    assert_eq!(
        r.co.adapter().injected().iter().filter(|a| matches!(a, Action::MoveTo { .. })).count(),
        1
    );
}

#[test]
fn an_older_or_duplicate_lossy_move_is_dropped() {
    let mut r = Rig::open_all();
    r.send(Body::Move { u: 0.1, v: 0.1 }); // motion seq 1
    r.motion_seq = 0;
    r.send(Body::Move { u: 0.2, v: 0.2 }); // motion seq 1 again
    assert_eq!(
        r.co.adapter().injected().iter().filter(|a| matches!(a, Action::MoveTo { .. })).count(),
        1
    );
}

// ── Capabilities ──────────────────────────────────────────────────────────

#[test]
fn a_capability_the_ticket_lacks_is_refused_per_event() {
    let mut r = Rig::open(&["pointer.move"]);
    let out = r.send(key("KeyA", true));
    assert!(out.iter().any(|o| matches!(
        o,
        Outbound::Status { status: Status::Error { code: "capability_denied" }, .. }
    )));
    r.send(click(true));
    assert!(r.co.adapter().injected().is_empty());
    r.send(Body::Move { u: 0.5, v: 0.5 });
    assert_eq!(r.co.adapter().injected().len(), 1, "what it does permit still works");
}

#[test]
fn a_button_needs_both_pointer_button_and_pointer_move() {
    let mut r = Rig::open(&["pointer.button"]);
    r.send(click(true));
    assert!(r.co.adapter().injected().is_empty());
}

#[test]
fn text_needs_keyboard_text_not_keyboard_physical() {
    let mut r = Rig::open(&["keyboard.physical"]);
    r.send(Body::Text { text: "hi".into() });
    assert!(r.co.adapter().injected().is_empty());
    let mut r = Rig::open(&["keyboard.text"]);
    r.send(Body::Text { text: "hi".into() });
    assert_eq!(r.co.adapter().injected().len(), 1);
}

#[test]
fn a_host_that_cannot_produce_a_key_pauses_instead_of_guessing() {
    let mut r = Rig::open_all();
    r.co.adapter_mut().unsupported_keys.push("IntlBackslash".into());
    let out = r.send(key("IntlBackslash", true));
    assert_eq!(stopped(&out), Some("unsupported_key"));
}

// ── Never-injectable chords ───────────────────────────────────────────────

#[test]
fn ctrl_alt_delete_and_win_l_and_the_stop_chord_are_never_injected() {
    let mut r = Rig::open_all();
    r.send(key("ControlLeft", true));
    r.send(key("AltLeft", true));
    let before = r.co.adapter().injected().len();
    let out = r.send(key("Delete", true));
    assert!(out.iter().any(|o| matches!(
        o,
        Outbound::Status { status: Status::Error { code: "prohibited_chord" }, .. }
    )));
    assert_eq!(r.co.adapter().injected().len(), before, "Delete was not injected");

    let mut r = Rig::open_all();
    r.send(key("MetaLeft", true));
    let before = r.co.adapter().injected().len();
    r.send(key("KeyL", true));
    assert_eq!(r.co.adapter().injected().len(), before, "Win+L refused");

    // Control: the same keys singly are fine, so the refusals above are real.
    let mut r = Rig::open_all();
    r.send(key("KeyL", true));
    r.send(key("Delete", true));
    assert_eq!(r.co.adapter().injected().len(), 2);
}

// ── Flooding (T14) ────────────────────────────────────────────────────────

#[test]
fn a_flood_past_the_rate_limit_ends_the_grant_and_stop_still_works() {
    let mut r = Rig::open_all();
    // Burst allowance is 240 and the clock does not move.
    let mut ended = false;
    for _ in 0..300 {
        let out = r.send(Body::Heartbeat { keys: vec![], buttons: vec![], last_ack: 0 });
        if stopped(&out) == Some("rate_exceeded") {
            ended = true;
            break;
        }
    }
    assert!(ended, "the burst ceiling was enforced");
    assert!(!r.co.is_controlled());
    // Stop remains callable and harmless afterwards.
    assert!(r.co.local_stop().is_empty());
}

#[test]
fn a_run_of_malformed_packets_ends_the_grant_but_one_does_not() {
    let mut r = Rig::open_all();
    let mut e = r.env(Body::Move { u: f64::NAN, v: 0.0 });
    e.seq = 1;
    let out = r.co.on_packet(r.from_controller(Topic::Motion, e));
    assert!(out.iter().any(|o| matches!(o, Outbound::Status { status: Status::Error { code: "out_of_range" }, .. })));
    assert!(r.co.is_controlled(), "one bad packet is not a violation");

    let mut ended = false;
    for _ in 0..20 {
        let mut e = r.env(Body::Move { u: f64::NAN, v: 0.0 });
        e.seq = 1;
        let out = r.co.on_packet(r.from_controller(Topic::Motion, e));
        if stopped(&out) == Some("protocol_violation") {
            ended = true;
            break;
        }
    }
    assert!(ended);
}

#[test]
fn undecodable_packets_count_toward_the_same_limit_but_only_from_the_controller() {
    let ctrl = Sender { identity: "ctrl".into(), sid: "PA_ctrl".into() };
    let stranger = Sender { identity: "stranger".into(), sid: "PA_x".into() };

    // One bad packet from the controller: an error status, grant intact.
    let mut r = Rig::open_all();
    let out = r.co.on_malformed(Some(&ctrl), "bad_json");
    assert!(out.iter().any(|o| matches!(
        o,
        Outbound::Status { status: Status::Error { code: "bad_json" }, to_identity, .. }
            if to_identity == "ctrl"
    )));
    assert!(r.co.is_controlled());

    // Any number from a stranger or from the server: no output, no effect.
    for _ in 0..100 {
        assert!(r.co.on_malformed(Some(&stranger), "bad_json").is_empty());
        assert!(r.co.on_malformed(None, "bad_json").is_empty());
    }
    assert!(r.co.is_controlled(), "a stranger cannot tear down someone else's grant");

    // A run from the controller ends it.
    let mut ended = false;
    for _ in 0..20 {
        if stopped(&r.co.on_malformed(Some(&ctrl), "bad_json")) == Some("protocol_violation") {
            ended = true;
            break;
        }
    }
    assert!(ended);
}

#[test]
fn queue_overflow_ends_the_grant() {
    let mut r = Rig::open_all();
    // Queue without pumping, using many distinct challenges' worth of time? The
    // rate limiter allows a 240 burst, so 129 reliable events fit the bucket.
    let mut ended = false;
    for _ in 0..200 {
        let e = r.env(key("KeyA", true));
        let out = r.co.on_packet(r.from_controller(Topic::Input, e));
        if stopped(&out) == Some("queue_overflow") {
            ended = true;
            break;
        }
    }
    assert!(ended);
    assert!(r.co.adapter().injected().is_empty(), "nothing queued ever ran");
}

// ── T10: reconnect, sleep, lock, display change ───────────────────────────

#[test]
fn every_transport_interruption_clears_authority_and_nothing_resumes() {
    for ev in [
        TransportEvent::Disconnected,
        TransportEvent::Reconnecting,
        TransportEvent::Reconnected,
        TransportEvent::Resumed,
        TransportEvent::ServerInactive,
    ] {
        let mut r = Rig::open_all();
        r.send(key("KeyA", true));
        let out = r.co.on_transport(ev.clone());
        assert!(stopped(&out).is_some(), "{ev:?}");
        assert!(!r.co.is_controlled());
        let before = r.calls().len();
        r.send(key("KeyB", true));
        assert_eq!(r.calls().len(), before, "no replay after {ev:?}");
    }
}

#[test]
fn locking_the_session_ends_control_and_unlocking_does_not_restore_it() {
    let mut r = Rig::open_all();
    let out = r.co.on_transport(TransportEvent::SessionLocked);
    assert_eq!(stopped(&out), Some("session_locked"));
    r.co.session_unlocked();
    assert!(!r.co.is_controlled());
}

#[test]
fn a_display_change_ends_control_and_withdraws_the_share_approval() {
    let mut r = Rig::open_all();
    let out = r.co.on_transport(TransportEvent::DisplayChanged);
    assert_eq!(stopped(&out), Some("capture_changed"));
    assert!(!r.co.may_join("room-1", "hb-1"), "sharing needs a fresh local approval");
}

#[test]
fn the_controller_or_companion_leaving_ends_control_but_a_bystander_does_not() {
    let mut r = Rig::open_all();
    assert!(r.co.on_transport(TransportEvent::ParticipantLeft { identity: "bystander".into() }).is_empty());
    assert!(r.co.is_controlled());
    let out = r.co.on_transport(TransportEvent::ParticipantLeft { identity: "ctrl".into() });
    assert_eq!(stopped(&out), Some("disconnected"));
}

// ── Held input ────────────────────────────────────────────────────────────

#[test]
fn a_confirmed_hold_survives_past_ten_seconds() {
    // The 10 s limit applies to holds with no fresh confirmation. A heartbeat
    // that still lists the key is a confirmation, so a long, healthy drag is
    // not cut off. (Without confirmation the 1 s heartbeat timeout ends the
    // grant long before 10 s, so the limit is defence in depth, not reachable
    // from this API alone.)
    let mut r = Rig::open_all();
    r.send(key("ShiftLeft", true));
    r.run_healthy(12_000, &["ShiftLeft"]);
    assert!(r.co.is_controlled());
    assert!(r.co.adapter().releases().is_empty());
}

#[test]
fn ending_control_does_not_cancel_a_modifier_the_owner_is_physically_holding() {
    let mut r = Rig::open_all();
    r.send(key("ShiftLeft", true));
    r.send(key("KeyA", true));
    r.co.adapter_mut().locally_held.push("ShiftLeft".into());
    r.co.local_stop();
    let rel = r.co.adapter().releases();
    assert!(rel.contains(&&Release::Key("KeyA".into())));
    assert!(
        !rel.contains(&&Release::Key("ShiftLeft".into())),
        "the owner's own Shift is theirs"
    );
}

#[test]
fn a_failing_adapter_ends_the_grant() {
    let mut r = Rig::open_all();
    r.co.adapter_mut().fail_inject = true;
    let out = r.send(key("KeyA", true));
    assert_eq!(stopped(&out), Some("adapter_failure"));
}

// ── Reset ─────────────────────────────────────────────────────────────────

#[test]
fn a_controller_reset_releases_everything_and_ends_the_epoch() {
    let mut r = Rig::open_all();
    r.send(key("KeyA", true));
    let out = r.send(Body::Reset { reason: ResetReason::Blur });
    assert_eq!(stopped(&out), Some("reset"));
    assert!(r.co.adapter().releases().contains(&&Release::Key("KeyA".into())));
    assert!(!r.co.is_controlled());
}

// ── Status is directed, never broadcast ───────────────────────────────────

#[test]
fn every_outbound_status_is_addressed_to_the_bound_controller_only() {
    let mut r = Rig::shared();
    r.activate_with(&ALL_CAPS).unwrap();
    let mut all = r.renew();
    all.extend(r.co.local_stop());
    assert!(!all.is_empty());
    for o in all {
        if let Outbound::Status { to_identity, grant_id, epoch, .. } = o {
            assert_eq!(to_identity, "ctrl");
            assert_eq!(grant_id, GRANT);
            assert_eq!(epoch, EPOCH);
        }
    }
}
