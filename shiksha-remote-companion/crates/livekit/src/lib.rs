//! LiveKit glue for the companion. Deliberately thin.
//!
//! Every decision lives in `shiksha-companion-core`; every byte-level rule in
//! `shiksha-companion-transport`. This crate only (a) joins a room, (b) turns SDK
//! events into `NetEvent`s, (c) publishes the packets that come back, each to
//! exactly one identity.
//!
//! **NOT YET COMPILED.** Written against signatures read from
//! `docs.rs/livekit/0.12.0` on 26 Nov 2026 (cited per item below), on a machine
//! that cannot build the SDK. Expect compile errors; they should be shallow.
//!
//! ## What this crate must never do
//! * join without a [`JoinPermit`] (a type only the core's physical-consent path
//!   can mint) — so an idle companion cannot join anything;
//! * subscribe to media. `auto_subscribe` is forced off and no track event is
//!   ever handled, so no sink is created. This is ADR-02 Option C: the token
//!   grants `can_subscribe`, so media isolation is *only* client-side, and this
//!   is where it is enforced;
//! * publish data without a destination, or to more than one.
//!
//! Screen publish (capture) is RC-013, not here.

use std::sync::{Arc, Mutex, MutexGuard};

use livekit::id::ParticipantIdentity;
use livekit::{DataPacket, Room, RoomError, RoomEvent, RoomOptions};
use shiksha_companion_core::adapter::InputAdapter;
use shiksha_companion_core::clock::{Entropy, MonotonicClock};
use shiksha_companion_core::companion::{JoinPermit, Sender, StopReason, TransportEvent};
use shiksha_companion_transport::session::{Driver, Effects, NetEvent, Packet};
use tokio::sync::mpsc::{Receiver, UnboundedReceiver};

/// The identity the server gave this companion: `rcdev_<host_binding_id>`
/// (`sessions_app/collaboration/companion_tokens.py::companion_identity`).
pub fn expected_identity(host_binding_id: &str) -> String {
    format!("rcdev_{host_binding_id}")
}

#[derive(Debug)]
pub enum JoinError {
    Connect(RoomError),
    /// Joined, but not the room the owner approved. Left again immediately.
    WrongRoom { expected: String, actual: String },
    /// Joined, but under an identity other than this binding's. Left again.
    WrongIdentity { expected: String, actual: String },
}

pub struct Joined {
    pub room: Room,
    pub events: UnboundedReceiver<RoomEvent>,
}

/// Join the room the owner approved sharing into.
///
/// Consumes the permit. The token's own room claim is the server's check; the
/// two comparisons after connecting are ours, so a token minted for the wrong
/// room or identity (a server bug, or a swapped token) is caught on the client
/// rather than trusted.
pub async fn join(permit: JoinPermit, url: &str, token: &str) -> Result<Joined, JoinError> {
    let mut options = RoomOptions::default();
    // `RoomOptions` is #[non_exhaustive] (docs.rs/.../struct.RoomOptions.html):
    // assign fields on a default, never a struct literal.
    options.auto_subscribe = false; // no media in, ever (ADR-02 Option C)
    options.adaptive_stream = false; // only meaningful for subscribed video
    options.dynacast = false;

    // docs.rs/.../struct.Room.html:
    //   pub async fn connect(url: &str, token: &str, options: RoomOptions)
    //       -> RoomResult<(Self, UnboundedReceiver<RoomEvent>)>
    let (room, events) =
        Room::connect(url, token, options).await.map_err(JoinError::Connect)?;

    let actual_room = room.name(); // pub fn name(&self) -> String
    if actual_room != permit.room_name() {
        let _ = room.close().await; // pub async fn close(&self) -> RoomResult<()>
        return Err(JoinError::WrongRoom {
            expected: permit.room_name().to_string(),
            actual: actual_room,
        });
    }
    let expected = expected_identity(permit.host_binding_id());
    // LocalParticipant::identity(&self) -> ParticipantIdentity; as_str() -> &str
    let actual = room.local_participant().identity();
    if actual.as_str() != expected {
        let actual = actual.as_str().to_string();
        let _ = room.close().await;
        return Err(JoinError::WrongIdentity { expected, actual });
    }
    Ok(Joined { room, events })
}

/// SDK event → neutral event. Anything not listed is ignored on purpose: in
/// particular **no track or publication event is handled**, so nothing ever
/// subscribes to or renders room media.
///
/// `RoomEvent` is #[non_exhaustive], hence the wildcard arm.
pub fn to_net_event(ev: RoomEvent) -> Option<NetEvent> {
    match ev {
        // DataReceived { payload: Arc<Vec<u8>>, topic: Option<String>,
        //                kind: DataPacketKind, participant: Option<RemoteParticipant> }
        RoomEvent::DataReceived { payload, topic, participant, .. } => Some(NetEvent::Data {
            // The SDK-authenticated sender, never anything inside the payload.
            // `None` is server-origin data, which the core ignores.
            sender: participant.map(|p| Sender {
                identity: p.identity().as_str().to_owned(),
                sid: p.sid().as_str().to_owned(),
            }),
            topic: topic.unwrap_or_default(),
            payload: payload.as_ref().clone(),
        }),
        // Both announce a (possibly new) SID for an identity. The core treats a
        // changed SID for the controller or the companion as loss of authority.
        RoomEvent::ParticipantConnected(p) | RoomEvent::ParticipantActive(p) => {
            Some(NetEvent::Transport(TransportEvent::ParticipantSid {
                identity: p.identity().as_str().to_owned(),
                sid: p.sid().as_str().to_owned(),
            }))
        }
        RoomEvent::ParticipantDisconnected(p) => {
            Some(NetEvent::Transport(TransportEvent::ParticipantLeft {
                identity: p.identity().as_str().to_owned(),
            }))
        }
        // Transparent SDK reconnect is loss of authority even if the room
        // identity is unchanged (remote-control-protocol.md §Stop and reconnection).
        RoomEvent::Reconnecting => Some(NetEvent::Transport(TransportEvent::Reconnecting)),
        RoomEvent::Reconnected => Some(NetEvent::Transport(TransportEvent::Reconnected)),
        RoomEvent::Disconnected { .. } => Some(NetEvent::Transport(TransportEvent::Disconnected)),
        _ => None,
    }
}

#[derive(Debug)]
pub enum PublishError {
    EmptyDestination,
    Room(RoomError),
}

/// Publish one packet to exactly one identity.
pub async fn publish(room: &Room, p: Packet) -> Result<(), PublishError> {
    if p.to_identity.is_empty() {
        // An empty list would be a room broadcast in LiveKit. Refuse it here as
        // well as in `lower`, because this is the last line before the wire.
        return Err(PublishError::EmptyDestination);
    }
    // `DataPacket` implements Default; fill fields rather than rely on the
    // struct being constructible by literal from outside the crate.
    let mut packet = DataPacket::default();
    packet.reliable = p.reliable();
    packet.payload = p.payload;
    packet.topic = Some(p.topic.to_string());
    packet.destination_identities = vec![ParticipantIdentity::from(p.to_identity)];
    // pub async fn publish_data(&self, packet: DataPacket) -> RoomResult<()>
    room.local_participant().publish_data(packet).await.map_err(PublishError::Room)
}

/// What a caller does with the server-side notices. They are metadata: by the
/// time one arrives the gate is already closed.
pub type OnRevoke = Box<dyn Fn(String, StopReason) + Send + Sync>;

type Shared<A, C, E> = Arc<Mutex<Driver<A, C, E>>>;

/// Lock the driver. A poisoned lock means a panic happened mid-decision, so the
/// state may be half-updated: close the gate before continuing.
fn lock<A, C, E>(d: &Shared<A, C, E>) -> MutexGuard<'_, Driver<A, C, E>>
where
    A: InputAdapter,
    C: MonotonicClock,
    E: Entropy,
{
    match d.lock() {
        Ok(g) => g,
        Err(poisoned) => {
            let mut g = poisoned.into_inner();
            let _ = g.companion_mut().local_stop();
            g
        }
    }
}

async fn dispatch(room: &Room, fx: Effects, on_revoke: &OnRevoke) -> usize {
    let mut failures = 0;
    for p in fx.packets {
        // Best-effort: status is advisory. The gate was closed before any
        // `stopped` was produced, so a failed send never leaves input open.
        if publish(room, p).await.is_err() {
            failures += 1;
        }
    }
    for (grant, reason) in fx.revokes {
        on_revoke(grant, reason);
    }
    failures
}

/// Pump SDK events into the driver and publish what it asks for, until the room
/// closes. `outbox` carries effects produced elsewhere — chiefly the watchdog
/// thread's `tick()` — so that publishing stays on this task.
///
/// Returns when the event stream ends, after telling the core the transport is
/// gone.
pub async fn run<A, C, E>(
    room: &Room,
    mut events: UnboundedReceiver<RoomEvent>,
    driver: Shared<A, C, E>,
    mut outbox: Receiver<Effects>,
    on_revoke: OnRevoke,
) where
    A: InputAdapter,
    C: MonotonicClock,
    E: Entropy,
{
    loop {
        tokio::select! {
            ev = events.recv() => match ev {
                Some(ev) => {
                    if let Some(net) = to_net_event(ev) {
                        // The guard is released before any `.await`.
                        let fx = lock(&driver).handle(net);
                        dispatch(room, fx, &on_revoke).await;
                    }
                }
                None => {
                    let fx = lock(&driver).handle(NetEvent::Transport(TransportEvent::Disconnected));
                    dispatch(room, fx, &on_revoke).await;
                    break;
                }
            },
            Some(fx) = outbox.recv() => {
                dispatch(room, fx, &on_revoke).await;
            }
        }
    }
}

/// One watchdog step: tick the driver and hand any packets to [`run`].
///
/// Call about every 50 ms from a thread that does no capture or network work.
/// `try_send` rather than `send`: if the publisher is wedged the queue fills and
/// we drop *status*, never block the thread that enforces deadlines. The gate
/// has already closed by then; dropped status only delays the controller's UI.
pub fn watchdog_step<A, C, E>(driver: &Shared<A, C, E>, tx: &tokio::sync::mpsc::Sender<Effects>)
where
    A: InputAdapter,
    C: MonotonicClock,
    E: Entropy,
{
    let fx = lock(driver).tick();
    if !fx.is_empty() {
        let _ = tx.try_send(fx);
    }
}

/// Lets the transport runtime's worker threads hand packets to [`run`].
///
/// A newtype because `EffectSink` (transport) and `Sender` (tokio) are both
/// foreign to this crate. `try_send`: a wedged publisher drops status rather than
/// blocking a worker; the gate was already closed before any `stopped` existed.
pub struct ChannelSink(pub tokio::sync::mpsc::Sender<Effects>);

impl shiksha_companion_transport::runtime::EffectSink for ChannelSink {
    fn emit(&self, fx: Effects) {
        let _ = self.0.try_send(fx);
    }
}
