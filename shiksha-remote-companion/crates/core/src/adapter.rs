//! The OS boundary, as the core sees it.
//!
//! "No adapter accepts network JSON directly. ValidatedEvent is constructible
//! only by core authorization/validation code." The constructor below is
//! `pub(crate)`; a platform crate can read an event but cannot fabricate one,
//! so the only road to an OS call runs through the gate.

use crate::wire::{Body, Button};

/// One thing to do to the desktop. Mirrors the wire kinds minus the ones that
/// are control messages (`reset`, `heartbeat`), which never reach an adapter.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    MoveTo { u: f64, v: f64 },
    Button { button: Button, down: bool, u: f64, v: f64 },
    Wheel { dx: f64, dy: f64, u: f64, v: f64 },
    Key { code: String, down: bool },
    Text { text: String },
}

/// Proof that the gate approved this action a moment ago.
#[derive(Clone, Debug, PartialEq)]
pub struct ValidatedEvent {
    action: Action,
}

impl ValidatedEvent {
    pub(crate) fn new(action: Action) -> Self {
        Self { action }
    }
    pub fn action(&self) -> &Action {
        &self.action
    }
}

/// Convert a decoded body into an action. `None` for control messages.
pub(crate) fn action_of(body: &Body) -> Option<Action> {
    Some(match body {
        Body::Move { u, v } => Action::MoveTo { u: *u, v: *v },
        Body::Button { button, down, u, v, .. } => {
            Action::Button { button: *button, down: *down, u: *u, v: *v }
        }
        Body::Wheel { dx, dy, u, v, .. } => Action::Wheel { dx: *dx, dy: *dy, u: *u, v: *v },
        Body::Key { code, down } => Action::Key { code: code.clone(), down: *down },
        Body::Text { text } => Action::Text { text: text.clone() },
        Body::Reset { .. } | Body::Heartbeat { .. } => return None,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Release {
    Key(String),
    Button(Button),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdapterError(pub &'static str);

pub trait InputAdapter {
    /// Whether the host can produce this physical key. A host reports its
    /// supported subset; an unsupported code pauses the grant.
    fn supports_key(&self, code: &str) -> bool;

    fn inject(&mut self, event: &ValidatedEvent) -> Result<(), AdapterError>;

    /// Release one remotely-owned key or button.
    fn release(&mut self, what: &Release) -> Result<(), AdapterError>;

    /// Is this key being held down by the **local** person right now?
    ///
    /// Asked before releasing a remote hold, so ending a session does not
    /// cancel a Shift the owner is physically pressing. Adapters that cannot
    /// tell must answer `false` and document it; the executor then releases.
    fn physically_held(&self, _code: &str) -> bool {
        false
    }
}

/// Everything a [`FakeInputAdapter`] was asked to do, in order. The tests' main
/// assertion is that this stays **empty** whenever the gate is closed.
#[derive(Clone, Debug, PartialEq)]
pub enum Call {
    Inject(Action),
    Release(Release),
}

/// Deterministic stand-in for the Windows adapter.
#[derive(Default)]
pub struct FakeInputAdapter {
    pub calls: Vec<Call>,
    pub unsupported_keys: Vec<String>,
    pub locally_held: Vec<String>,
    pub fail_inject: bool,
}

impl FakeInputAdapter {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn injected(&self) -> Vec<&Action> {
        self.calls
            .iter()
            .filter_map(|c| match c {
                Call::Inject(a) => Some(a),
                _ => None,
            })
            .collect()
    }
    pub fn releases(&self) -> Vec<&Release> {
        self.calls
            .iter()
            .filter_map(|c| match c {
                Call::Release(r) => Some(r),
                _ => None,
            })
            .collect()
    }
}

impl InputAdapter for FakeInputAdapter {
    fn supports_key(&self, code: &str) -> bool {
        !self.unsupported_keys.iter().any(|k| k == code)
    }
    fn inject(&mut self, event: &ValidatedEvent) -> Result<(), AdapterError> {
        if self.fail_inject {
            return Err(AdapterError("fake_inject_failure"));
        }
        self.calls.push(Call::Inject(event.action().clone()));
        Ok(())
    }
    fn release(&mut self, what: &Release) -> Result<(), AdapterError> {
        self.calls.push(Call::Release(what.clone()));
        Ok(())
    }
    fn physically_held(&self, code: &str) -> bool {
        self.locally_held.iter().any(|k| k == code)
    }
}

/// See the `test-support` feature in Cargo.toml. Absent from every production build.
#[cfg(feature = "test-support")]
pub mod test_support {
    use super::{Action, ValidatedEvent};

    pub fn validated(action: Action) -> ValidatedEvent {
        ValidatedEvent::new(action)
    }
}
