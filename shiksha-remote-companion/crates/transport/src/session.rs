//! SDK-neutral glue between the network and the core.
//!
//! The LiveKit crate turns SDK events into [`NetEvent`]s and publishes the
//! [`Packet`]s this returns. Nothing here knows about LiveKit, so this layer is
//! testable (and reviewable) without an SDK, a room or a network.
//!
//! What this layer guarantees, by construction:
//! * every outgoing packet has exactly one destination identity; an empty one is
//!   dropped and counted, never widened to a broadcast;
//! * only the two input topics are decoded; any other topic is ignored here and
//!   never reaches the core;
//! * a packet the decoder refuses is reported to the core as malformed *with its
//!   authenticated sender*, so the core can ignore strangers and count the
//!   controller.

use shiksha_companion_core::adapter::InputAdapter;
use shiksha_companion_core::clock::{Entropy, MonotonicClock};
use shiksha_companion_core::companion::{
    Companion, Inbound, Outbound, Sender, StopReason, Topic, TransportEvent,
};
use shiksha_companion_core::wire::{TOPIC_INPUT, TOPIC_MOTION, TOPIC_STATUS};

use crate::codec::{decode_input, encode_status};

/// What the SDK layer reports. `sender` is what the SDK authenticated for a
/// data packet; `None` is server-origin data.
#[derive(Clone, Debug)]
pub enum NetEvent {
    Data { sender: Option<Sender>, topic: String, payload: Vec<u8> },
    Transport(TransportEvent),
}

/// One directed data packet to publish. `reliable` is implied by the topic:
/// status is the reliable lane.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Packet {
    pub to_identity: String,
    pub topic: &'static str,
    pub payload: Vec<u8>,
}

impl Packet {
    pub fn reliable(&self) -> bool {
        // Only status leaves the companion today, and it is reliable.
        self.topic == TOPIC_STATUS
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Effects {
    pub packets: Vec<Packet>,
    /// Best-effort notices for the server (`reason` is metadata, not authority:
    /// the gate was already closed when these were produced).
    pub revokes: Vec<(String, StopReason)>,
    /// Status packets dropped for lack of a destination. Should always be zero.
    pub dropped: u32,
}

impl Effects {
    pub fn is_empty(&self) -> bool {
        self.packets.is_empty() && self.revokes.is_empty() && self.dropped == 0
    }
    pub fn extend(&mut self, other: Effects) {
        self.packets.extend(other.packets);
        self.revokes.extend(other.revokes);
        self.dropped += other.dropped;
    }
}

/// Turn core output into wire packets.
pub fn lower(outs: Vec<Outbound>) -> Effects {
    let mut fx = Effects::default();
    for o in outs {
        match o {
            Outbound::Status { to_identity, grant_id, epoch, status } => {
                if to_identity.is_empty() {
                    fx.dropped += 1;
                    continue;
                }
                fx.packets.push(Packet {
                    to_identity,
                    topic: TOPIC_STATUS,
                    payload: encode_status(&grant_id, &epoch, &status),
                });
            }
            Outbound::RevokeToServer { grant_id, reason } => fx.revokes.push((grant_id, reason)),
        }
    }
    fx
}

fn topic_of(name: &str) -> Option<Topic> {
    match name {
        TOPIC_MOTION => Some(Topic::Motion),
        TOPIC_INPUT => Some(Topic::Input),
        _ => None,
    }
}

pub struct Driver<A: InputAdapter, C: MonotonicClock, E: Entropy> {
    companion: Companion<A, C, E>,
}

impl<A: InputAdapter, C: MonotonicClock, E: Entropy> Driver<A, C, E> {
    pub fn new(companion: Companion<A, C, E>) -> Self {
        Self { companion }
    }

    pub fn companion(&self) -> &Companion<A, C, E> {
        &self.companion
    }

    pub fn companion_mut(&mut self) -> &mut Companion<A, C, E> {
        &mut self.companion
    }

    /// One network event in; packets and notices out. Runs the injection pump
    /// after a packet so a decoded event is acted on (or refused) immediately.
    pub fn handle(&mut self, ev: NetEvent) -> Effects {
        match ev {
            NetEvent::Transport(t) => lower(self.companion.on_transport(t)),
            NetEvent::Data { sender, topic, payload } => {
                let Some(t) = topic_of(&topic) else {
                    return Effects::default(); // not ours (e.g. file topics)
                };
                match decode_input(&topic, &payload) {
                    Ok(envelope) => {
                        let mut outs =
                            self.companion.on_packet(Inbound { sender, topic: t, envelope });
                        outs.extend(self.companion.pump());
                        lower(outs)
                    }
                    Err(e) => lower(self.companion.on_malformed(sender.as_ref(), e.reason())),
                }
            }
        }
    }

    /// Periodic work: deadlines, challenges, acks. Drive from a thread that is
    /// independent of capture and network queues.
    pub fn tick(&mut self) -> Effects {
        let mut outs = self.companion.tick();
        outs.extend(self.companion.pump());
        lower(outs)
    }
}
