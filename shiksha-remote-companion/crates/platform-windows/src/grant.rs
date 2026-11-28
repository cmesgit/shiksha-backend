//! The things that must surround a grant, built in the only order that is safe.
//!
//! Nothing here decides anything: the rules are in `core` (`ConsentDialog`,
//! `Companion`). This module owns the *lifetimes* so the app cannot get the order
//! wrong:
//!
//! 1. [`GrantKit::new`] registers the physical Stop chord. If that fails there is
//!    no kit, hence no way to ask for consent: no Stop, no control.
//! 2. [`GrantKit::ask`] starts the input monitor *before* the dialog opens (the
//!    dialog judges "was that click physical?" from it), shows the dialog, and on
//!    Allow opens the always-visible status window **before returning**. So by the
//!    time the caller can activate anything, the indicator, the chord and the
//!    monitor all exist, and there is no activation path without them.
//! 3. The caller activates, then [`GrantGuard::arm`]s, then calls
//!    [`GrantGuard::poll`] often and feeds each event to [`apply`].
//! 4. Dropping the guard removes the indicator and the monitor.
//!
//! 🔴 Not run: no window has ever been drawn and no hook installed. The pure part
//! (`events`, `apply`) has tests that have compiled but not run.

use shiksha_companion_core::adapter::InputAdapter;
use shiksha_companion_core::clock::{Entropy, MonotonicClock};
use shiksha_companion_core::companion::{Companion, Outbound, TransportEvent};

/// What the guard noticed since the last poll.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum GuardEvent {
    /// The Stop chord was pressed. The latch is already closed.
    StopChord,
    /// The Stop button on the status window was pressed. The latch is already closed.
    StopButton,
    /// The owner used the keyboard or mouse themselves.
    PhysicalActivity,
}

#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct Signals {
    pub chord: bool,
    pub button: bool,
    pub activity: bool,
}

/// Turn raw signals into events. Stops come **first**: pressing the Stop button or
/// the chord is itself physical input, and the grant should record that the owner
/// stopped it, not merely that they touched the machine.
pub fn events(s: Signals) -> Vec<GuardEvent> {
    let mut v = Vec::new();
    if s.chord {
        v.push(GuardEvent::StopChord);
    }
    if s.button {
        v.push(GuardEvent::StopButton);
    }
    if s.activity {
        v.push(GuardEvent::PhysicalActivity);
    }
    v
}

/// Feed an event to the companion and return what it wants sent. Every event ends
/// authority; none can widen it. Idempotent in the companion, so a stop followed
/// by activity is safe.
pub fn apply<A: InputAdapter, C: MonotonicClock, E: Entropy>(
    companion: &mut Companion<A, C, E>,
    ev: GuardEvent,
) -> Vec<Outbound> {
    match ev {
        GuardEvent::StopChord | GuardEvent::StopButton => companion.sync_after_latch_stop(),
        GuardEvent::PhysicalActivity => companion.on_transport(TransportEvent::PhysicalActivity),
    }
}

#[cfg(all(windows, target_pointer_width = "64"))]
pub use native::{AskError, Approved, GrantGuard, GrantKit};

#[cfg(all(windows, target_pointer_width = "64"))]
mod native {
    use super::*;
    use crate::emergency::{EmergencyStop, HotkeyError};
    use crate::provenance::{MonitorError, PhysicalMonitor, PhysicalState};
    use crate::window::{run_consent, status_text, StatusWindow, WindowError};
    use shiksha_companion_core::companion::StopLatch;
    use shiksha_companion_core::consent::{
        ConsentDialog, ControlPrompt, Decision, OpenRefusal, Outcome,
    };
    use std::sync::Arc;

    #[derive(Debug, PartialEq, Eq)]
    pub enum AskError {
        /// The dialog refused to open (policy unknown, public broadcast, grant live…).
        Refused(OpenRefusal),
        /// Without the input monitor Allow cannot be told from an injected click.
        Monitor(MonitorError),
        Window(WindowError),
        /// The owner said no, or the dialog timed out. Nothing was activated.
        Declined { outcome: Outcome, refused_approvals: u32 },
        /// Allowed, but the indicator could not be shown, so nothing may activate.
        NoIndicator(WindowError),
    }

    /// Owns the Stop chord for as long as sharing can be controlled.
    pub struct GrantKit {
        latch: Arc<StopLatch>,
        chord: Arc<EmergencyStop>,
        physical: Arc<PhysicalState>,
    }

    pub struct Approved {
        pub decision: Decision,
        /// Allow attempts refused for being injected or early. Worth reporting.
        pub refused_approvals: u32,
        pub guard: GrantGuard,
    }

    impl GrantKit {
        /// `physical` must be the same state the input adapter reads held
        /// modifiers from. `Err` means there is no effective emergency Stop: do not
        /// offer control, and leave `Companion::set_emergency_stop_ready(false)`.
        pub fn new(latch: Arc<StopLatch>, physical: Arc<PhysicalState>) -> Result<Self, HotkeyError> {
            let chord = EmergencyStop::register_default(latch.clone())?;
            Ok(Self { latch, chord: Arc::new(chord), physical })
        }

        /// The chord, as the owner is told it.
        pub fn stop_shortcut(&self) -> String {
            self.chord.shortcut_text()
        }

        /// Has the chord fired while no grant was active? The latch is already closed.
        pub fn take_chord_idle(&self) -> bool {
            self.chord.take_fired()
        }

        /// Ask the owner. Blocks until decided; call on a thread that may own a
        /// window. `grant_active` must be true if any grant can inject right now
        /// (the dialog then refuses to open).
        ///
        /// The prompt's `stop_shortcut` is overwritten with the real chord so the
        /// dialog can never promise a key that is not registered.
        pub fn ask<C: MonotonicClock + Copy + 'static>(
            &self,
            clock: C,
            mut prompt: ControlPrompt,
            grant_active: bool,
        ) -> Result<Approved, AskError> {
            prompt.stop_shortcut = self.chord.shortcut_text();
            let name = prompt.controller_name.clone();
            let shortcut = prompt.stop_shortcut.clone();

            let monitor = PhysicalMonitor::start_with(self.physical.clone()).map_err(AskError::Monitor)?;
            let dialog = ConsentDialog::open(clock.now(), prompt, grant_active).map_err(AskError::Refused)?;
            let res = run_consent(dialog, clock, monitor.state()).map_err(AskError::Window)?;

            let decision = match (res.outcome, res.decision) {
                (Outcome::Allowed, Some(d)) => d,
                (o, _) => return Err(AskError::Declined { outcome: o, refused_approvals: res.refused_approvals }),
            };

            // The indicator comes before any authority exists.
            let status = StatusWindow::open(self.latch.clone(), status_text(&name, &shortcut))
                .map_err(AskError::NoIndicator)?;

            Ok(Approved {
                decision,
                refused_approvals: res.refused_approvals,
                guard: GrantGuard { status, monitor, chord: self.chord.clone(), physical: self.physical.clone() },
            })
        }
    }

    /// Alive from Allow until the grant ends. Drop it to remove the indicator and
    /// the monitor.
    pub struct GrantGuard {
        status: StatusWindow,
        // Declared after `status` so the indicator goes first, the monitor last.
        monitor: PhysicalMonitor,
        chord: Arc<EmergencyStop>,
        physical: Arc<PhysicalState>,
    }

    impl GrantGuard {
        /// Call once, right after activation succeeds. The owner's own clicks while
        /// answering the dialog, and anything they did during the activation
        /// request, happened before any input could be injected, so they must not
        /// end the grant that was just approved.
        pub fn arm(&self) {
            let _ = self.physical.take_activity();
        }

        /// Poll often (the watchdog tick is a good place).
        pub fn poll(&self) -> Vec<GuardEvent> {
            events(Signals {
                chord: self.chord.take_fired(),
                button: self.status.take_stop_pressed(),
                activity: self.physical.take_activity(),
            })
        }

        pub fn set_status_text(&self, t: String) {
            self.status.set_text(t);
        }

        pub fn monitor_state(&self) -> Arc<PhysicalState> {
            self.monitor.state()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use shiksha_companion_core::adapter::FakeInputAdapter;
    use shiksha_companion_core::clock::{CountingEntropy, FakeClock};
    use shiksha_companion_core::companion::{DeviceIdentity, StopLatch};

    #[test]
    fn nothing_noticed_means_no_events() {
        assert!(events(Signals::default()).is_empty());
    }

    #[test]
    fn stops_come_before_activity() {
        let v = events(Signals { chord: true, button: true, activity: true });
        assert_eq!(v, vec![GuardEvent::StopChord, GuardEvent::StopButton, GuardEvent::PhysicalActivity]);
    }

    #[test]
    fn activity_alone_is_reported() {
        assert_eq!(events(Signals { activity: true, ..Default::default() }), vec![GuardEvent::PhysicalActivity]);
    }

    #[test]
    fn events_on_an_idle_companion_change_nothing() {
        let mut c = Companion::new(
            FakeInputAdapter::default(),
            FakeClock::new(),
            CountingEntropy::default(),
            StopLatch::new(),
            DeviceIdentity { device_id: String::new(), key_thumbprint: String::new() },
        );
        for ev in [GuardEvent::StopChord, GuardEvent::StopButton, GuardEvent::PhysicalActivity] {
            assert!(apply(&mut c, ev).is_empty(), "{ev:?} sent something with no grant");
        }
    }
}
