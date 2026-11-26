//! The companion's authorisation state machine.
//!
//! One struct owns everything that can lead to an OS call: the local consent
//! latch, the activation, the lease, the freshness challenges, the sequence
//! floors, the bounded queue and the set of remotely-held keys. The only way an
//! `Action` reaches an adapter is [`Companion::pump`], and `pump` re-checks the
//! whole gate immediately before each call.
//!
//! ## Gate (all must hold, every event)
//! 1. native consent latch set for this epoch  (`StopLatch` generation unchanged)
//! 2. server state active                      (no inactive notice received)
//! 3. activation valid                         (monotonic deadline from `exp`)
//! 4. lease fresh                              (`LeaseTracker`)
//! 5. local session unlocked
//! 6. capture descriptor current
//! 7. controller heartbeat fresh               (≤ 1 s)
//! 8. sender / sequence valid                  (per packet, in `on_packet`)
//! 9. capabilities permit the event            (per packet)
//!
//! ## What deliberately does not exist
//! Pause-and-resume. A pause ends the epoch; coming back is a *new* activation
//! with a new epoch and fresh local consent ("paused cannot survive…").

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use crate::adapter::{action_of, Action, InputAdapter, Release, ValidatedEvent};
use crate::clock::{b64url, Entropy, Mono, MonotonicClock};
use crate::lease::{LeaseError, LeaseRequest, LeaseTracker};
use crate::ticket::{
    check_header, validate_activation, ActivationClaims, ActivationExpect, KeySet, LeaseClaims,
    TicketError, VerifiedHeader, ACTIVATION_TYP, LEASE_TYP,
};
use crate::wire::{Body, Button, Envelope};

// ── Constants from the protocol ───────────────────────────────────────────
pub const CHALLENGE_INTERVAL_MS: u64 = 250;
pub const CHALLENGE_VALID_MS: u64 = 1_000;
pub const CHALLENGES_RETAINED: usize = 4;
pub const HEARTBEAT_TIMEOUT_MS: u64 = 1_000;
pub const ACK_INTERVAL_MS: u64 = 100;
pub const MAX_HOLD_MS: u64 = 10_000;
pub const QUEUE_LIMIT: usize = 128;
/// How long a grant may sit without its first lease before it is abandoned.
pub const FIRST_LEASE_GRACE_MS: u64 = 3_000;
pub const MAX_CONSECUTIVE_MALFORMED: u32 = 16;

const INPUT_RATE_PER_SEC: u64 = 120;
const INPUT_BURST: u64 = 240;
const MOTION_RATE_PER_SEC: u64 = 60;
const MOTION_BURST: u64 = 60;

// ── Stop latch ────────────────────────────────────────────────────────────

/// The atomic generation latch the UI and watchdog threads write and the
/// injection path reads "immediately before every OS injection".
///
/// Stopping is `fetch_add`: lock-free, so a physical Stop works even if the
/// thread holding the companion is stuck. An epoch is armed with the generation
/// current at consent time; any later stop makes the two differ, permanently.
pub struct StopLatch(AtomicU64);

impl StopLatch {
    pub fn new() -> Arc<Self> {
        Arc::new(Self(AtomicU64::new(0)))
    }
    pub fn generation(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
    /// Close the gate. Returns the new generation.
    pub fn stop(&self) -> u64 {
        self.0.fetch_add(1, Ordering::SeqCst) + 1
    }
}

// ── Local provenance ──────────────────────────────────────────────────────

/// Where a UI event came from. The Windows adapter marks its own `SendInput`
/// events, so remote input can never answer a native consent dialog.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum InputOrigin {
    Physical,
    Injected,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    NotPhysical,
    NoHostApproval,
    NoCapture,
    GrantInProgress,
    Ticket(TicketError),
    AlreadyApproved,
}

// ── Join policy ───────────────────────────────────────────────────────────

/// Capability to join a LiveKit room. Only [`Companion::approve_share`] makes
/// one, and only after a physical confirmation, so "join only a locally
/// approved host binding, never at idle startup" is a type, not a convention.
#[derive(Debug, PartialEq, Eq)]
pub struct JoinPermit {
    room_name: String,
    host_binding_id: String,
}

impl JoinPermit {
    pub fn room_name(&self) -> &str {
        &self.room_name
    }
    pub fn host_binding_id(&self) -> &str {
        &self.host_binding_id
    }
}

/// What the screen-only publish produced, reported after the track exists.
#[derive(Clone, Debug)]
pub struct Capture {
    pub track_sid: String,
    pub display_id: String,
    pub capture_generation: u64,
    pub companion_identity: String,
    pub companion_sid: String,
}

struct HostState {
    room_name: String,
    host_binding_id: String,
    capture: Option<Capture>,
}

/// This installation's own identity.
#[derive(Clone, Debug)]
pub struct DeviceIdentity {
    pub device_id: String,
    pub key_thumbprint: String,
}

// ── Inputs and outputs ────────────────────────────────────────────────────

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sender {
    pub identity: String,
    pub sid: String,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Topic {
    Motion,
    Input,
}

/// A decoded data packet. `sender` is what the **SDK** authenticated, not
/// anything in the payload; `None` means server-origin data, which is ignored.
#[derive(Clone, Debug)]
pub struct Inbound {
    pub sender: Option<Sender>,
    pub topic: Topic,
    pub envelope: Envelope,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TransportEvent {
    Disconnected,
    Reconnecting,
    /// The SDK reconnected. Treated as loss of authority even when the room
    /// identity is unchanged.
    Reconnected,
    /// Sleep/resume: leases and pending packets are discarded.
    Resumed,
    ParticipantSid { identity: String, sid: String },
    ParticipantLeft { identity: String },
    SessionLocked,
    /// Resize, rotation, topology change or track replacement.
    DisplayChanged,
    /// Account switch, logout, device unlink or OS user switch.
    AccountChanged,
    /// A server notice that the grant is no longer active. Not authority to
    /// open anything, only to close.
    ServerInactive,
    /// The owner physically used the keyboard or mouse. "Local input priority":
    /// remote injection pauses and remote-owned states are released. The adapter
    /// reports only input the OS marks as *not* injected, so remote input cannot
    /// trigger it, and it carries no key values.
    PhysicalActivity,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum StopReason {
    LocalStop,
    StaleLease,
    ActivationExpired,
    HeartbeatTimeout,
    SequenceGap,
    QueueOverflow,
    RateExceeded,
    Disconnected,
    SidChanged,
    CaptureChanged,
    SessionLocked,
    ServerInactive,
    ControllerReset,
    UnsupportedKey,
    StaleInput,
    AdapterFailure,
    ProtocolViolation,
    AccountChanged,
    LocalActivity,
}

impl StopReason {
    pub fn code(self) -> &'static str {
        match self {
            StopReason::LocalStop => "local_stop",
            StopReason::StaleLease => "stale_lease",
            StopReason::ActivationExpired => "activation_expired",
            StopReason::HeartbeatTimeout => "heartbeat_timeout",
            StopReason::SequenceGap => "sequence_gap",
            StopReason::QueueOverflow => "queue_overflow",
            StopReason::RateExceeded => "rate_exceeded",
            StopReason::Disconnected => "disconnected",
            StopReason::SidChanged => "sid_changed",
            StopReason::CaptureChanged => "capture_changed",
            StopReason::SessionLocked => "session_locked",
            StopReason::ServerInactive => "server_inactive",
            StopReason::ControllerReset => "reset",
            StopReason::UnsupportedKey => "unsupported_key",
            StopReason::StaleInput => "stale_input",
            StopReason::AdapterFailure => "adapter_failure",
            StopReason::ProtocolViolation => "protocol_violation",
            StopReason::AccountChanged => "account_changed",
            StopReason::LocalActivity => "local_activity",
        }
    }
}

/// `shiksha.rc.status.v1` bodies (native → bound controller).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Status {
    Ready { capture_generation: u64, track_sid: String },
    Challenge { nonce: String, valid_for_ms: u64 },
    Ack { input_seq: u64, motion_seq: u64 },
    Stopped { reason: &'static str },
    Error { code: &'static str },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outbound {
    /// Directed at exactly one identity; the transport must never broadcast.
    Status { to_identity: String, grant_id: String, epoch: String, status: Status },
    /// Best-effort metadata for the server. Never required for safety: the
    /// gate is already closed by the time this is produced.
    RevokeToServer { grant_id: String, reason: StopReason },
}

// ── Internals ─────────────────────────────────────────────────────────────

struct Queued {
    action: Action,
    valid_until: Mono,
    seq: u64,
}

struct Bucket {
    milli: u64,
    cap: u64,
    per_ms: u64,
    at: Mono,
}

impl Bucket {
    fn new(rate_per_sec: u64, burst: u64, now: Mono) -> Self {
        Self { milli: burst * 1000, cap: burst * 1000, per_ms: rate_per_sec, at: now }
    }
    fn take(&mut self, now: Mono) -> bool {
        self.milli = (self.milli + now.since(self.at) * self.per_ms).min(self.cap);
        self.at = now;
        if self.milli >= 1000 {
            self.milli -= 1000;
            true
        } else {
            false
        }
    }
}

struct Active {
    claims: ActivationClaims,
    armed_generation: u64,
    activated_at: Mono,
    activation_deadline: Mono,
    lease: LeaseTracker,
    controller_sid: String,
    server_active: bool,
    ready: bool,
    heartbeat_deadline: Mono,
    challenges: VecDeque<(String, Mono)>,
    last_challenge_at: Mono,
    reliable_last: u64,
    applied_exec: u64,
    motion_last: u64,
    motion_floor: u64,
    queue: VecDeque<Queued>,
    owned_keys: Vec<(String, Mono)>,
    owned_buttons: Vec<(Button, Mono)>,
    input_bucket: Bucket,
    motion_bucket: Bucket,
    malformed_run: u32,
    last_ack_at: Mono,
    last_ack: (u64, u64),
}

enum Closed {
    /// Authority is gone; end the epoch.
    End(StopReason),
    /// Not open *yet* (waiting for the first lease). Not a reason to end.
    NotYet,
}

pub struct Companion<A: InputAdapter, C: MonotonicClock, E: Entropy> {
    adapter: A,
    clock: C,
    entropy: E,
    latch: Arc<StopLatch>,
    device: DeviceIdentity,
    host: Option<HostState>,
    active: Option<Active>,
    session_locked: bool,
}

fn has(caps: &[String], c: &str) -> bool {
    caps.iter().any(|x| x == c)
}

impl<A: InputAdapter, C: MonotonicClock, E: Entropy> Companion<A, C, E> {
    pub fn new(
        adapter: A,
        clock: C,
        entropy: E,
        latch: Arc<StopLatch>,
        device: DeviceIdentity,
    ) -> Self {
        Self {
            adapter,
            clock,
            entropy,
            latch,
            device,
            host: None,
            active: None,
            session_locked: false,
        }
    }

    /// Handle for the UI/hotkey/watchdog threads. `stop()` on it is lock-free.
    pub fn stop_handle(&self) -> Arc<StopLatch> {
        self.latch.clone()
    }

    pub fn adapter(&self) -> &A {
        &self.adapter
    }

    pub fn adapter_mut(&mut self) -> &mut A {
        &mut self.adapter
    }

    pub fn is_controlled(&self) -> bool {
        self.active.is_some()
    }

    // ── Local consent ────────────────────────────────────────────────────

    /// The owner confirmed, physically, that this device may **share** its
    /// screen into this room. Not consent to control.
    pub fn approve_share(
        &mut self,
        origin: InputOrigin,
        room_name: &str,
        host_binding_id: &str,
    ) -> Result<JoinPermit, Refusal> {
        if origin != InputOrigin::Physical {
            return Err(Refusal::NotPhysical);
        }
        if self.active.is_some() {
            // No new consent dialog opens while a grant can inject.
            return Err(Refusal::GrantInProgress);
        }
        self.host = Some(HostState {
            room_name: room_name.to_string(),
            host_binding_id: host_binding_id.to_string(),
            capture: None,
        });
        Ok(JoinPermit {
            room_name: room_name.to_string(),
            host_binding_id: host_binding_id.to_string(),
        })
    }

    /// May the transport join this room right now? `false` at idle startup and
    /// for any room or binding the owner did not approve.
    pub fn may_join(&self, room_name: &str, host_binding_id: &str) -> bool {
        matches!(&self.host, Some(h) if h.room_name == room_name && h.host_binding_id == host_binding_id)
    }

    /// The screen track exists. Records what an activation ticket must match.
    pub fn capture_published(&mut self, capture: Capture) -> Result<(), Refusal> {
        match self.host.as_mut() {
            Some(h) => {
                h.capture = Some(capture);
                Ok(())
            }
            None => Err(Refusal::NoHostApproval),
        }
    }

    /// Latch consent for one grant and epoch.
    ///
    /// `origin` must be `Physical`. `request_start` is the monotonic time the
    /// activation request was **sent**; the activation deadline is measured from
    /// it, so a slow reply shortens the window. `activation_hash` is the
    /// transport's SHA-256 of the ticket, which every lease must echo.
    #[allow(clippy::too_many_arguments)]
    pub fn activate(
        &mut self,
        origin: InputOrigin,
        header: &VerifiedHeader,
        keys: &KeySet,
        claims: ActivationClaims,
        activation_hash: String,
        approved_capabilities: Vec<String>,
        wall_now_unix: i64,
        request_start: Mono,
    ) -> Result<(), Refusal> {
        if origin != InputOrigin::Physical {
            return Err(Refusal::NotPhysical);
        }
        if self.active.is_some() {
            return Err(Refusal::GrantInProgress);
        }
        let host = self.host.as_ref().ok_or(Refusal::NoHostApproval)?;
        let capture = host.capture.as_ref().ok_or(Refusal::NoCapture)?;

        check_header(header, ACTIVATION_TYP, keys).map_err(Refusal::Ticket)?;
        let expect = ActivationExpect {
            room_name: host.room_name.clone(),
            device_id: self.device.device_id.clone(),
            device_thumbprint: self.device.key_thumbprint.clone(),
            host_binding_id: host.host_binding_id.clone(),
            capture_generation: capture.capture_generation,
            track_sid: capture.track_sid.clone(),
            display_id: capture.display_id.clone(),
            companion_identity: capture.companion_identity.clone(),
            companion_sid: capture.companion_sid.clone(),
            approved_capabilities,
        };
        validate_activation(&claims, &expect, wall_now_unix).map_err(Refusal::Ticket)?;

        // Wall clock is used exactly once: to turn `exp` into "this many
        // milliseconds from the moment the request left".
        let remaining_s = (claims.exp - wall_now_unix).max(0) as u64;
        let now = self.clock.now();
        let armed = self.latch.generation();
        let lease = LeaseTracker::new(
            claims.grant_id.clone(),
            claims.epoch.clone(),
            claims.device_id.clone(),
            activation_hash,
            claims.grant_version,
        );
        self.active = Some(Active {
            controller_sid: claims.controller_sid.clone(),
            activation_deadline: request_start.plus_ms(remaining_s.saturating_mul(1000)),
            claims,
            armed_generation: armed,
            activated_at: now,
            lease,
            server_active: true,
            ready: false,
            heartbeat_deadline: now.plus_ms(HEARTBEAT_TIMEOUT_MS),
            challenges: VecDeque::new(),
            last_challenge_at: now,
            reliable_last: 0,
            applied_exec: 0,
            motion_last: 0,
            motion_floor: 0,
            queue: VecDeque::new(),
            owned_keys: Vec::new(),
            owned_buttons: Vec::new(),
            input_bucket: Bucket::new(INPUT_RATE_PER_SEC, INPUT_BURST, now),
            motion_bucket: Bucket::new(MOTION_RATE_PER_SEC, MOTION_BURST, now),
            malformed_run: 0,
            last_ack_at: now,
            last_ack: (0, 0),
        });
        Ok(())
    }

    // ── Lease ────────────────────────────────────────────────────────────

    /// A fresh random nonce, drawn **before** an `activate`/`resume` request is
    /// sent so it can ride in `lease_nonce` (api-contracts.md §Control grants).
    pub fn fresh_nonce(&mut self) -> String {
        b64url(&self.entropy.bytes16())
    }

    /// Adopt the activation request as the outstanding lease request, so the
    /// first lease arriving in its reply is accepted against `lease_nonce` and
    /// timed from `request_start`. Call after [`Self::activate`], then feed the
    /// reply's `lease_jwt` to [`Self::on_lease`].
    pub fn adopt_activation_lease(&mut self, lease_nonce: String, request_start: Mono) -> bool {
        let generation = self.latch.generation();
        match self.active.as_mut() {
            Some(a) if a.armed_generation == generation => {
                a.lease.adopt(lease_nonce, request_start, generation);
                true
            }
            _ => false,
        }
    }

    /// Start a renewal. The app POSTs the returned nonce, epoch and version and
    /// feeds the signed reply to [`Self::on_lease`]. Call about once a second.
    pub fn begin_lease(&mut self) -> Option<LeaseRequest> {
        let now = self.clock.now();
        let generation = self.latch.generation();
        let a = self.active.as_mut()?;
        if a.armed_generation != generation {
            return None;
        }
        Some(a.lease.begin(now, generation, &mut self.entropy))
    }

    /// A signature-verified lease reply. The first accepted one opens the gate
    /// and produces `ready` plus the first challenge.
    pub fn on_lease(
        &mut self,
        header: &VerifiedHeader,
        keys: &KeySet,
        claims: &LeaseClaims,
    ) -> Result<Vec<Outbound>, LeaseError> {
        let now = self.clock.now();
        let generation = self.latch.generation();
        check_header(header, LEASE_TYP, keys).map_err(LeaseError::Claims)?;
        let a = self.active.as_mut().ok_or(LeaseError::NoOutstandingRequest)?;
        a.lease.accept(claims, now, generation)?;

        let mut out = Vec::new();
        if !a.ready {
            a.ready = true;
            // The controller only starts after `ready`, so its 1 s heartbeat window
            // starts now, not at activation. Otherwise a slow first lease (up to the
            // 1 s reply cap) would eat the window and end a healthy grant.
            a.heartbeat_deadline = now.plus_ms(HEARTBEAT_TIMEOUT_MS);
            out.push(Self::status_to(
                a,
                Status::Ready {
                    capture_generation: a.claims.capture_generation,
                    track_sid: a.claims.track_sid.clone(),
                },
            ));
            let nonce = Self::new_challenge(a, now, &mut self.entropy);
            out.push(Self::status_to(
                a,
                Status::Challenge { nonce, valid_for_ms: CHALLENGE_VALID_MS },
            ));
        }
        Ok(out)
    }

    // ── Gate ─────────────────────────────────────────────────────────────

    fn gate(&self, a: &Active, now: Mono) -> Result<(), Closed> {
        if self.latch.generation() != a.armed_generation {
            return Err(Closed::End(StopReason::LocalStop));
        }
        if !a.server_active {
            return Err(Closed::End(StopReason::ServerInactive));
        }
        if self.session_locked {
            return Err(Closed::End(StopReason::SessionLocked));
        }
        if now >= a.activation_deadline {
            return Err(Closed::End(StopReason::ActivationExpired));
        }
        if !a.ready {
            // Waiting on the first lease. Abandon if it never comes.
            if now.since(a.activated_at) > FIRST_LEASE_GRACE_MS {
                return Err(Closed::End(StopReason::StaleLease));
            }
            return Err(Closed::NotYet);
        }
        if !a.lease.is_fresh(now) {
            return Err(Closed::End(StopReason::StaleLease));
        }
        if now >= a.heartbeat_deadline {
            return Err(Closed::End(StopReason::HeartbeatTimeout));
        }
        Ok(())
    }

    // ── Ending authority ─────────────────────────────────────────────────

    /// Close the gate, discard queued work, release what the remote side holds,
    /// *then* tell the controller. Order matters: the acknowledgement is only
    /// honest once release has run.
    fn end(&mut self, reason: StopReason) -> Vec<Outbound> {
        // 1. Close the gate before anything else can observe it open.
        self.latch.stop();
        let Some(mut a) = self.active.take() else {
            return Vec::new();
        };
        // 2. Discard queued input.
        a.queue.clear();
        // 3. Release owned states, sparing anything the local user is holding.
        for (code, _) in a.owned_keys.drain(..) {
            if !self.adapter.physically_held(&code) {
                let _ = self.adapter.release(&Release::Key(code));
            }
        }
        for (button, _) in a.owned_buttons.drain(..) {
            let _ = self.adapter.release(&Release::Button(button));
        }
        // 4. Only now acknowledge.
        let mut out = vec![Outbound::Status {
            to_identity: a.claims.controller_identity.clone(),
            grant_id: a.claims.grant_id.clone(),
            epoch: a.claims.epoch.clone(),
            status: Status::Stopped { reason: reason.code() },
        }];
        out.push(Outbound::RevokeToServer { grant_id: a.claims.grant_id.clone(), reason });
        out
    }

    /// Physical Stop / hotkey. Honoured whatever its origin: stopping is never
    /// the dangerous direction.
    pub fn local_stop(&mut self) -> Vec<Outbound> {
        self.end(StopReason::LocalStop)
    }

    /// The UI thread already closed the latch lock-free; run the cleanup.
    pub fn sync_after_latch_stop(&mut self) -> Vec<Outbound> {
        match &self.active {
            Some(a) if a.armed_generation != self.latch.generation() => {
                self.end(StopReason::LocalStop)
            }
            _ => Vec::new(),
        }
    }

    pub fn on_transport(&mut self, ev: TransportEvent) -> Vec<Outbound> {
        match ev {
            TransportEvent::Disconnected
            | TransportEvent::Reconnecting
            | TransportEvent::Reconnected
            | TransportEvent::Resumed => self.end(StopReason::Disconnected),
            TransportEvent::ServerInactive => self.end(StopReason::ServerInactive),
            TransportEvent::PhysicalActivity => self.end(StopReason::LocalActivity),
            TransportEvent::SessionLocked => {
                self.session_locked = true;
                self.end(StopReason::SessionLocked)
            }
            TransportEvent::DisplayChanged => {
                let out = self.end(StopReason::CaptureChanged);
                // The descriptor no longer describes the screen: sharing needs a
                // fresh local approval too.
                self.host = None;
                out
            }
            TransportEvent::AccountChanged => {
                let out = self.end(StopReason::AccountChanged);
                self.host = None;
                out
            }
            TransportEvent::ParticipantSid { identity, sid } => {
                let stop = match &self.active {
                    Some(a) => {
                        (identity == a.claims.controller_identity && sid != a.controller_sid)
                            || (identity == a.claims.companion_identity
                                && sid != a.claims.companion_sid)
                    }
                    None => false,
                };
                if stop {
                    self.end(StopReason::SidChanged)
                } else {
                    Vec::new()
                }
            }
            TransportEvent::ParticipantLeft { identity } => {
                let stop = matches!(&self.active, Some(a)
                    if identity == a.claims.controller_identity
                        || identity == a.claims.companion_identity);
                if stop {
                    self.end(StopReason::Disconnected)
                } else {
                    Vec::new()
                }
            }
        }
    }

    /// The local session was unlocked. Does **not** restore anything.
    pub fn session_unlocked(&mut self) {
        self.session_locked = false;
    }

    // ── Inbound packets ──────────────────────────────────────────────────

    pub fn on_packet(&mut self, p: Inbound) -> Vec<Outbound> {
        let now = self.clock.now();
        let Some(a) = self.active.as_ref() else {
            return Vec::new(); // no grant: zero effect, zero output
        };
        // Server-origin data is never input.
        let Some(sender) = p.sender.as_ref() else {
            return Vec::new();
        };
        // The SDK-authenticated sender must be the bound controller at its
        // current SID. Anything else is a stranger and is ignored, not obeyed
        // and not allowed to disturb the grant.
        if sender.identity != a.claims.controller_identity || sender.sid != a.controller_sid {
            return Vec::new();
        }
        let e = &p.envelope;
        if e.grant_id != a.claims.grant_id || e.epoch != a.claims.epoch {
            return Vec::new(); // replay under another grant/epoch
        }

        match self.gate(a, now) {
            Ok(()) => {}
            Err(Closed::NotYet) => return Vec::new(),
            Err(Closed::End(r)) => return self.end(r),
        }

        let a = self.active.as_mut().expect("checked above");
        let to = a.claims.controller_identity.clone();
        let (gid, ep) = (a.claims.grant_id.clone(), a.claims.epoch.clone());
        let err = |code: &'static str| Outbound::Status {
            to_identity: to.clone(),
            grant_id: gid.clone(),
            epoch: ep.clone(),
            status: Status::Error { code },
        };

        if let Err(rej) = e.validate() {
            a.malformed_run += 1;
            if a.malformed_run >= MAX_CONSECUTIVE_MALFORMED {
                return self.end(StopReason::ProtocolViolation);
            }
            return vec![err(rej.reason())];
        }
        if e.capture_generation != a.claims.capture_generation {
            return vec![err("stale_capture")];
        }
        if e.body.is_motion() != (p.topic == Topic::Motion) {
            a.malformed_run += 1;
            return vec![err("wrong_topic")];
        }
        a.malformed_run = 0;

        // Freshness: the packet must echo a still-valid native challenge.
        let valid_until = match a
            .challenges
            .iter()
            .find(|(n, until)| n == &e.challenge && now < *until)
        {
            Some((_, until)) => *until,
            None => return vec![err("stale_challenge")],
        };

        if p.topic == Topic::Motion {
            return self.on_motion(e, now, valid_until, &err);
        }
        self.on_reliable(e, now, valid_until, &err)
    }

    /// A packet the decoder refused. Counts toward the same consecutive-malformed
    /// limit as one that decoded but failed validation, so a controller cannot
    /// dodge the limit by sending garbage the decoder rejects first. Packets
    /// from anyone other than the bound controller are ignored entirely: a
    /// stranger must not be able to disturb, or even answer, the grant.
    pub fn on_malformed(&mut self, sender: Option<&Sender>, reason: &'static str) -> Vec<Outbound> {
        let Some(a) = self.active.as_mut() else {
            return Vec::new();
        };
        let from_controller = matches!(sender,
            Some(s) if s.identity == a.claims.controller_identity && s.sid == a.controller_sid);
        if !from_controller {
            return Vec::new();
        }
        a.malformed_run += 1;
        if a.malformed_run >= MAX_CONSECUTIVE_MALFORMED {
            return self.end(StopReason::ProtocolViolation);
        }
        vec![Self::status_to(a, Status::Error { code: reason })]
    }

    fn on_motion(
        &mut self,
        e: &Envelope,
        now: Mono,
        valid_until: Mono,
        err: &dyn Fn(&'static str) -> Outbound,
    ) -> Vec<Outbound> {
        let a = self.active.as_mut().expect("active");
        // Lossy lane: old or duplicate moves are simply dropped, and a move at
        // or below the floor a reliable action already applied cannot drag the
        // cursor backwards.
        if e.seq <= a.motion_last || e.seq <= a.motion_floor {
            return Vec::new();
        }
        if !a.motion_bucket.take(now) {
            return Vec::new(); // coalesce under flood; motion is disposable
        }
        a.motion_last = e.seq;
        if !has(&a.claims.capabilities, "pointer.move") {
            return vec![err("capability_denied")];
        }
        let Some(action) = action_of(&e.body) else {
            return Vec::new();
        };
        // "Coalesce unsent motion": a newer move replaces an unsent older one.
        if let Some(last) = a.queue.back_mut() {
            if matches!(last.action, Action::MoveTo { .. }) {
                *last = Queued { action, valid_until, seq: last.seq };
                return Vec::new();
            }
        }
        if a.queue.len() >= QUEUE_LIMIT {
            return self.end(StopReason::QueueOverflow);
        }
        a.queue.push_back(Queued { action, valid_until, seq: a.reliable_last });
        Vec::new()
    }

    fn on_reliable(
        &mut self,
        e: &Envelope,
        now: Mono,
        valid_until: Mono,
        err: &dyn Fn(&'static str) -> Outbound,
    ) -> Vec<Outbound> {
        let a = self.active.as_mut().expect("active");
        if !a.input_bucket.take(now) {
            return self.end(StopReason::RateExceeded);
        }
        if e.seq <= a.reliable_last {
            // Duplicate: acknowledged by the next ack, never re-injected.
            return Vec::new();
        }
        if e.seq != a.reliable_last + 1 {
            return self.end(StopReason::SequenceGap);
        }
        a.reliable_last = e.seq;

        match &e.body {
            Body::Reset { .. } => self.end(StopReason::ControllerReset),
            Body::Heartbeat { keys, buttons, .. } => {
                a.heartbeat_deadline = now.plus_ms(HEARTBEAT_TIMEOUT_MS);
                self.reconcile(keys, buttons, now);
                Vec::new()
            }
            body => {
                let caps = &a.claims.capabilities;
                let allowed = match body {
                    Body::Key { .. } => has(caps, "keyboard.physical"),
                    Body::Text { .. } => has(caps, "keyboard.text"),
                    Body::Button { .. } => {
                        has(caps, "pointer.button") && has(caps, "pointer.move")
                    }
                    Body::Wheel { .. } => has(caps, "pointer.scroll") && has(caps, "pointer.move"),
                    _ => false,
                };
                if !allowed {
                    return vec![err("capability_denied")];
                }
                if let Body::Key { code, .. } = body {
                    if !self.adapter.supports_key(code) {
                        return self.end(StopReason::UnsupportedKey);
                    }
                }
                let a = self.active.as_mut().expect("active");
                if let Body::Button { motion_seq, .. } | Body::Wheel { motion_seq, .. } = body {
                    a.motion_floor = a.motion_floor.max(*motion_seq);
                }
                if a.queue.len() >= QUEUE_LIMIT {
                    return self.end(StopReason::QueueOverflow);
                }
                let action = action_of(body).expect("not a control message");
                a.queue.push_back(Queued { action, valid_until, seq: e.seq });
                Vec::new()
            }
        }
    }

    /// Compare the controller's claimed held state with what we own. Never
    /// synthesises a key-down: a heartbeat can only release.
    fn reconcile(&mut self, keys: &[String], buttons: &[Button], now: Mono) {
        let Some(a) = self.active.as_mut() else { return };
        let adapter = &mut self.adapter;
        a.owned_keys.retain_mut(|(code, confirmed)| {
            if keys.iter().any(|k| k == code) {
                *confirmed = now;
                true
            } else {
                if !adapter.physically_held(code) {
                    let _ = adapter.release(&Release::Key(code.clone()));
                }
                false
            }
        });
        a.owned_buttons.retain_mut(|(b, confirmed)| {
            if buttons.contains(b) {
                *confirmed = now;
                true
            } else {
                let _ = adapter.release(&Release::Button(*b));
                false
            }
        });
    }

    // ── The only road to an OS call ──────────────────────────────────────

    /// Run queued actions. Each one re-checks the whole gate and its own
    /// freshness immediately before the adapter is called.
    pub fn pump(&mut self) -> Vec<Outbound> {
        let mut out = Vec::new();
        loop {
            let now = self.clock.now();
            let Some(a) = self.active.as_ref() else { break };
            if a.queue.is_empty() {
                break;
            }
            if let Err(Closed::End(r)) = self.gate(a, now) {
                out.extend(self.end(r));
                break;
            }
            if matches!(self.gate(a, now), Err(Closed::NotYet)) {
                break;
            }
            let a = self.active.as_mut().expect("active");
            let q = a.queue.pop_front().expect("non-empty");
            if now >= q.valid_until {
                // An event from before its challenge lapsed. Dropping it alone
                // would tear a down from its up, so end the epoch instead.
                out.extend(self.end(StopReason::StaleInput));
                break;
            }
            if let Action::Key { code, down: true } = &q.action {
                if prohibited_chord(code, a.owned_keys.iter().map(|(c, _)| c.as_str())) {
                    let a = self.active.as_ref().expect("active");
                    out.push(Outbound::Status {
                        to_identity: a.claims.controller_identity.clone(),
                        grant_id: a.claims.grant_id.clone(),
                        epoch: a.claims.epoch.clone(),
                        status: Status::Error { code: "prohibited_chord" },
                    });
                    continue;
                }
            }
            let event = ValidatedEvent::new(q.action.clone());
            if self.adapter.inject(&event).is_err() {
                out.extend(self.end(StopReason::AdapterFailure));
                break;
            }
            let a = self.active.as_mut().expect("active");
            match &q.action {
                Action::Key { code, down: true } => {
                    if !a.owned_keys.iter().any(|(c, _)| c == code) {
                        a.owned_keys.push((code.clone(), now));
                    }
                }
                Action::Key { code, down: false } => a.owned_keys.retain(|(c, _)| c != code),
                Action::Button { button, down: true, .. } => {
                    if !a.owned_buttons.iter().any(|(b, _)| b == button) {
                        a.owned_buttons.push((*button, now));
                    }
                }
                Action::Button { button, down: false, .. } => {
                    a.owned_buttons.retain(|(b, _)| b != button)
                }
                _ => {}
            }
            if q.seq > a.applied_exec {
                a.applied_exec = q.seq;
            }
        }
        out
    }

    // ── Periodic work ────────────────────────────────────────────────────

    /// Call at least every ~50 ms from a task that is independent of capture
    /// and network queues (the watchdog requirement).
    pub fn tick(&mut self) -> Vec<Outbound> {
        let now = self.clock.now();
        let Some(a) = self.active.as_ref() else {
            return Vec::new();
        };
        match self.gate(a, now) {
            Err(Closed::End(r)) => return self.end(r),
            Err(Closed::NotYet) => return Vec::new(),
            Ok(()) => {}
        }

        let a = self.active.as_mut().expect("active");
        let mut out = Vec::new();

        // Held-input limit: no hold survives 10 s without fresh confirmation.
        let adapter = &mut self.adapter;
        a.owned_keys.retain(|(code, confirmed)| {
            if now.since(*confirmed) > MAX_HOLD_MS {
                if !adapter.physically_held(code) {
                    let _ = adapter.release(&Release::Key(code.clone()));
                }
                false
            } else {
                true
            }
        });
        a.owned_buttons.retain(|(b, confirmed)| {
            if now.since(*confirmed) > MAX_HOLD_MS {
                let _ = adapter.release(&Release::Button(*b));
                false
            } else {
                true
            }
        });

        a.challenges.retain(|(_, until)| now < *until);
        if now.since(a.last_challenge_at) >= CHALLENGE_INTERVAL_MS {
            let nonce = Self::new_challenge(a, now, &mut self.entropy);
            out.push(Self::status_to(
                a,
                Status::Challenge { nonce, valid_for_ms: CHALLENGE_VALID_MS },
            ));
        }

        let applied = if a.queue.is_empty() { a.reliable_last } else { a.applied_exec };
        let ack = (applied, a.motion_floor.max(a.motion_last));
        if ack != a.last_ack && now.since(a.last_ack_at) >= ACK_INTERVAL_MS {
            a.last_ack = ack;
            a.last_ack_at = now;
            out.push(Self::status_to(a, Status::Ack { input_seq: ack.0, motion_seq: ack.1 }));
        }
        out
    }

    fn new_challenge(a: &mut Active, now: Mono, entropy: &mut E) -> String {
        let nonce = b64url(&entropy.bytes16());
        a.challenges.push_back((nonce.clone(), now.plus_ms(CHALLENGE_VALID_MS)));
        while a.challenges.len() > CHALLENGES_RETAINED {
            a.challenges.pop_front();
        }
        a.last_challenge_at = now;
        nonce
    }

    fn status_to(a: &Active, status: Status) -> Outbound {
        Outbound::Status {
            to_identity: a.claims.controller_identity.clone(),
            grant_id: a.claims.grant_id.clone(),
            epoch: a.claims.epoch.clone(),
            status,
        }
    }

    /// Challenge nonces currently valid, newest last. Exposed so a test (and a
    /// real controller harness) can echo one.
    pub fn live_challenges(&self) -> Vec<String> {
        let now = self.clock.now();
        self.active
            .as_ref()
            .map(|a| {
                a.challenges
                    .iter()
                    .filter(|(_, until)| now < *until)
                    .map(|(n, _)| n.clone())
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// Chords that are never injectable, evaluated against the *resulting* pressed
/// set: Ctrl+Alt+Delete, Win+L (and Meta+L generally) and the native Stop chord
/// Ctrl+Alt+Shift+F12.
fn prohibited_chord<'a>(incoming: &'a str, owned: impl Iterator<Item = &'a str>) -> bool {
    let mut ctrl = false;
    let mut alt = false;
    let mut shift = false;
    let mut meta = false;
    let mut delete = false;
    let mut l = false;
    let mut f12 = false;
    for k in owned.chain(std::iter::once(incoming)) {
        match k {
            "ControlLeft" | "ControlRight" => ctrl = true,
            "AltLeft" | "AltRight" => alt = true,
            "ShiftLeft" | "ShiftRight" => shift = true,
            "MetaLeft" | "MetaRight" => meta = true,
            "Delete" => delete = true,
            "KeyL" => l = true,
            "F12" => f12 = true,
            _ => {}
        }
    }
    (ctrl && alt && delete) || (meta && l) || (ctrl && alt && shift && f12)
}

#[cfg(test)]
mod unit {
    use super::*;

    #[test]
    fn chords_are_judged_on_the_resulting_set() {
        let none: [&str; 0] = [];
        assert!(!prohibited_chord("KeyL", none.iter().copied()));
        assert!(prohibited_chord("KeyL", ["MetaLeft"].into_iter()));
        assert!(prohibited_chord("Delete", ["ControlLeft", "AltRight"].into_iter()));
        assert!(!prohibited_chord("Delete", ["ControlLeft"].into_iter()));
        assert!(prohibited_chord("F12", ["ControlLeft", "AltLeft", "ShiftLeft"].into_iter()));
        assert!(!prohibited_chord("F12", ["ControlLeft", "AltLeft"].into_iter()));
    }

    #[test]
    fn bucket_allows_burst_then_throttles_then_refills() {
        let mut b = Bucket::new(120, 240, Mono(0));
        for _ in 0..240 {
            assert!(b.take(Mono(0)));
        }
        assert!(!b.take(Mono(0)));
        // 100 ms refills 12 tokens.
        for _ in 0..12 {
            assert!(b.take(Mono(100)));
        }
        assert!(!b.take(Mono(100)));
    }

    #[test]
    fn latch_generation_only_moves_forward() {
        let l = StopLatch::new();
        assert_eq!(l.generation(), 0);
        assert_eq!(l.stop(), 1);
        assert_eq!(l.stop(), 2);
        assert_eq!(l.generation(), 2);
    }
}
