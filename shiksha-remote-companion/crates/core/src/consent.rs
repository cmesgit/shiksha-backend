//! The native consent dialog, as a state machine with no window in it.
//!
//! Every behavioural requirement on the owner's consent lives here, so it can be
//! type-checked and tested without a screen; the Win32 layer only *renders*
//! [`ConsentView`] and forwards events. Requirements, from
//! `permission-security-model.md` (T22), `frontend-ux-specification.md`,
//! `companion-architecture.md` and `security-test-plan.md` (T13):
//!
//! * the dialog opens with **Deny focused and Allow disabled**;
//! * **Enter and Space cannot approve** before the dwell has elapsed — Allow is not
//!   even focusable until then;
//! * approval needs a **physical** click or key. An injected one changes nothing;
//! * **Escape and the close button deny**;
//! * **no auto-approve**, and no countdown that approves. The only timer that ends
//!   the dialog *denies* (the 30 s approval deadline);
//! * it refuses to open while a grant can inject ("no new consent dialog opens while
//!   another grant can inject"), when the recording policy is unknown, and for a
//!   public broadcast;
//! * the copy names the real capability, in plain language.
//!
//! 🔴 What this does **not** do: nothing here makes consent *free*. Every
//! cryptographic and UI invariant holds perfectly while a pressured owner clicks
//! Allow. These checks are evidence about the dialog; the scripted peer-pressure
//! walkthrough judged by a safeguarding owner (T22) is the control that matters,
//! and it has not happened.

use crate::clock::Mono;
use crate::companion::InputOrigin;

/// How long Allow stays disabled after the dialog appears.
///
/// ⚠ **DECISION NEEDED — not an engineering choice.** The pack requires "a minimum
/// dwell" (T22) and gives no number. 3 s is a placeholder chosen to be long enough
/// to read one sentence and short enough not to be bypassed by habit. The
/// safeguarding owner should set it, and the browser dialog (`ControlRequestDialog`)
/// should use the same value.
pub const CONSENT_DWELL_MS: u64 = 3_000;

/// "native physical Allow or Deny; 30s deadline" (frontend-ux-specification.md).
/// Expiry **denies**.
pub const APPROVAL_DEADLINE_MS: u64 = 30_000;

/// A click or key counts as physical only if the owner's real hardware produced an
/// input event this recently. See [`PhysicalCorrelator`].
pub const PHYSICAL_CORRELATION_MS: u64 = 500;

/// Said once, in these words, on every control prompt. It is the "plain-language
/// capability copy" of T22: the real power, not a euphemism.
pub const POWER_STATEMENT: &str = "They can use the mouse and keyboard in any application you can open, including your files and terminals.";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecordingStatus {
    /// Verified by the server.
    Known { recording: bool },
    /// Not verified. Control is refused: "refuse MVP activation if policy unknown".
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ControlPrompt {
    /// The server-verified name of the person asking. Never a self-asserted one.
    pub controller_name: String,
    /// Profile/context line, e.g. "Teacher". Empty if none.
    pub controller_context: String,
    pub meeting_title: String,
    pub device_name: String,
    pub display_label: String,
    /// Subset of the five contract capabilities, as the owner approved in the
    /// browser. Never widened here.
    pub capabilities: Vec<String>,
    /// How the owner stops it, shown verbatim, e.g. "Ctrl+Alt+Shift+F12".
    pub stop_shortcut: String,
    pub recording: RecordingStatus,
    /// A public broadcast / spectator context: control is not offered at all.
    pub public_broadcast: bool,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum OpenRefusal {
    /// A grant can inject right now. Close it first: a dialog opened under live
    /// injection could be answered by the very input it is asking about.
    GrantActive,
    PolicyUnknown,
    PublicBroadcast,
    NoCapabilities,
    NoName,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Focus {
    Deny,
    Allow,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum DenyReason {
    Button,
    Escape,
    Close,
    /// The 30 s deadline passed. Expiry denies; it never approves.
    Expired,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Pending,
    Allowed,
    Denied(DenyReason),
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Control {
    Allow,
    Deny,
    Close,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum KeyPress {
    Enter,
    Space,
    Tab,
    ShiftTab,
    Escape,
    Other,
}

/// What the renderer draws. The renderer may not add a default button, a
/// countdown-to-approve, or anything this does not say.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConsentView {
    /// Accessible name of the dialog (also its visible title).
    pub title: String,
    /// Body lines in reading order; the accessible description is these joined.
    pub lines: Vec<String>,
    pub allow_enabled: bool,
    pub allow_label: String,
    pub deny_label: String,
    pub focus: Focus,
    pub outcome: Outcome,
}

/// Plain-language names for the five contract capabilities. Unknown names are
/// shown verbatim rather than dropped, so a capability can never be granted
/// without being named.
pub fn capability_copy(cap: &str) -> String {
    match cap {
        "pointer.move" => "move the pointer".to_string(),
        "pointer.button" => "click the mouse buttons".to_string(),
        "pointer.scroll" => "scroll".to_string(),
        "keyboard.physical" => "press keys on the keyboard".to_string(),
        "keyboard.text" => "type text".to_string(),
        other => other.to_string(),
    }
}

fn join_and(items: &[String]) -> String {
    match items.len() {
        0 => String::new(),
        1 => items[0].clone(),
        2 => format!("{} and {}", items[0], items[1]),
        _ => format!("{}, and {}", items[..items.len() - 1].join(", "), items[items.len() - 1]),
    }
}

pub struct ConsentDialog {
    prompt: ControlPrompt,
    /// `Some` for a pairing/share confirmation: fixed title and lines instead of the
    /// control prompt's. Every rule below (dwell, physical-only approval, deny on
    /// timeout, no default Allow) applies identically.
    custom: Option<(String, Vec<String>)>,
    opened: Mono,
    focus: Focus,
    outcome: Outcome,
    /// Allow clicks/keys refused for being injected or too early: an audit signal
    /// that something tried to approve on the owner's behalf.
    refused_approvals: u32,
}

impl ConsentDialog {
    /// Open the dialog. `grant_active` must be true while any grant can inject.
    pub fn open(now: Mono, prompt: ControlPrompt, grant_active: bool) -> Result<Self, OpenRefusal> {
        if grant_active {
            return Err(OpenRefusal::GrantActive);
        }
        if prompt.public_broadcast {
            return Err(OpenRefusal::PublicBroadcast);
        }
        if prompt.recording == RecordingStatus::Unknown {
            return Err(OpenRefusal::PolicyUnknown);
        }
        if prompt.capabilities.is_empty() {
            return Err(OpenRefusal::NoCapabilities);
        }
        if prompt.controller_name.trim().is_empty() {
            return Err(OpenRefusal::NoName);
        }
        Ok(Self {
            prompt,
            custom: None,
            opened: now,
            focus: Focus::Deny,
            outcome: Outcome::Pending,
            refused_approvals: 0,
        })
    }

    /// A pairing or share-approval confirmation. Same state machine, different
    /// words: "Pairing confirmation names the account, not merely 'Device
    /// connected'" (frontend-ux-specification.md). `decision()` for these carries no
    /// capabilities; the caller acts on `outcome()`.
    pub fn open_confirmation(
        now: Mono,
        title: String,
        lines: Vec<String>,
        grant_active: bool,
    ) -> Result<Self, OpenRefusal> {
        if grant_active {
            return Err(OpenRefusal::GrantActive);
        }
        if title.trim().is_empty() || lines.is_empty() {
            return Err(OpenRefusal::NoName);
        }
        Ok(Self {
            prompt: ControlPrompt {
                controller_name: String::new(),
                controller_context: String::new(),
                meeting_title: String::new(),
                device_name: String::new(),
                display_label: String::new(),
                capabilities: Vec::new(),
                stop_shortcut: String::new(),
                recording: RecordingStatus::Unknown,
                public_broadcast: false,
            },
            custom: Some((title, lines)),
            opened: now,
            focus: Focus::Deny,
            outcome: Outcome::Pending,
            refused_approvals: 0,
        })
    }

    fn dwell_left_ms(&self, now: Mono) -> u64 {
        CONSENT_DWELL_MS.saturating_sub(now.since(self.opened))
    }

    pub fn allow_enabled(&self, now: Mono) -> bool {
        self.outcome == Outcome::Pending && self.dwell_left_ms(now) == 0
    }

    pub fn outcome(&self) -> Outcome {
        self.outcome
    }

    pub fn refused_approvals(&self) -> u32 {
        self.refused_approvals
    }

    pub fn view(&self, now: Mono) -> ConsentView {
        if let Some((title, lines)) = &self.custom {
            return self.view_with(now, title.clone(), lines.clone());
        }
        let p = &self.prompt;
        let who = if p.controller_context.trim().is_empty() {
            p.controller_name.clone()
        } else {
            format!("{} ({})", p.controller_name, p.controller_context)
        };
        let caps: Vec<String> = p.capabilities.iter().map(|c| capability_copy(c)).collect();
        let mut lines = vec![
            format!(
                "{who} is asking to control {} — {}, in \u{201c}{}\u{201d}.",
                p.device_name, p.display_label, p.meeting_title
            ),
            format!("If you allow it, they will be able to {}.", join_and(&caps)),
            POWER_STATEMENT.to_string(),
            "Other people who can see this screen may see what they do. Sending files needs a separate approval.".to_string(),
        ];
        if let RecordingStatus::Known { recording } = p.recording {
            lines.push(if recording {
                "This session is being recorded.".to_string()
            } else {
                "This session is not being recorded.".to_string()
            });
        }
        lines.push(format!("You can stop it at any time with {} or the Stop button.", p.stop_shortcut));
        self.view_with(now, format!("Allow {} to control this computer?", p.controller_name), lines)
    }

    fn view_with(&self, now: Mono, title: String, lines: Vec<String>) -> ConsentView {
        let left = self.dwell_left_ms(now);
        let allow_label = if self.outcome != Outcome::Pending || left == 0 {
            "Allow".to_string()
        } else {
            // Rounds up so it never shows "0s" while still disabled.
            format!("Allow (in {}s)", left.div_ceil(1000))
        };
        ConsentView {
            title,
            lines,
            allow_enabled: self.allow_enabled(now),
            allow_label,
            deny_label: "Deny".to_string(),
            focus: self.focus,
            outcome: self.outcome,
        }
    }

    /// Advance time. The only thing that can end a pending dialog by itself is the
    /// deadline, and it **denies**.
    pub fn tick(&mut self, now: Mono) {
        if self.outcome == Outcome::Pending && now.since(self.opened) >= APPROVAL_DEADLINE_MS {
            self.outcome = Outcome::Denied(DenyReason::Expired);
        }
    }

    fn deny(&mut self, why: DenyReason) {
        if self.outcome == Outcome::Pending {
            self.outcome = Outcome::Denied(why);
        }
    }

    /// Try to approve. Refused (and counted) unless the dwell has passed **and**
    /// the input was physical.
    fn try_allow(&mut self, now: Mono, origin: InputOrigin) {
        if self.outcome != Outcome::Pending {
            return;
        }
        if origin != InputOrigin::Physical || !self.allow_enabled(now) {
            self.refused_approvals += 1;
            return;
        }
        self.outcome = Outcome::Allowed;
    }

    /// A mouse click on a control. `origin` is how the platform classified the
    /// event (see [`PhysicalCorrelator`]).
    ///
    /// Denying is accepted from any origin: it is the safe direction, and refusing
    /// it would let a flood of injected input keep a dialog open. Only **Allow**
    /// needs provenance.
    pub fn on_click(&mut self, now: Mono, which: Control, origin: InputOrigin) {
        self.tick(now);
        match which {
            Control::Allow => self.try_allow(now, origin),
            Control::Deny => self.deny(DenyReason::Button),
            Control::Close => self.deny(DenyReason::Close),
        }
    }

    pub fn on_key(&mut self, now: Mono, key: KeyPress, origin: InputOrigin) {
        self.tick(now);
        if self.outcome != Outcome::Pending {
            return;
        }
        match key {
            KeyPress::Escape => self.deny(DenyReason::Escape),
            // Tab only reaches Allow once it is enabled; before that, focus cannot
            // leave Deny, so a stray Enter or Space can only ever deny.
            KeyPress::Tab | KeyPress::ShiftTab => {
                self.focus = if self.focus == Focus::Deny && self.allow_enabled(now) {
                    Focus::Allow
                } else {
                    Focus::Deny
                };
            }
            KeyPress::Enter | KeyPress::Space => match self.focus {
                Focus::Deny => self.deny(DenyReason::Button),
                Focus::Allow => self.try_allow(now, origin),
            },
            KeyPress::Other => {}
        }
    }

    /// The decision, once made. Only an `Allowed` dialog yields one, and it carries
    /// the capabilities the owner saw — never more.
    pub fn decision(&self) -> Option<Decision> {
        match self.outcome {
            Outcome::Allowed => Some(Decision {
                origin: InputOrigin::Physical,
                capabilities: self.prompt.capabilities.clone(),
            }),
            _ => None,
        }
    }
}

/// What to hand to `Companion::activate` after an Allow.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    pub origin: InputOrigin,
    pub capabilities: Vec<String>,
}

/// Decides whether a dialog event was the owner's hand.
///
/// The OS marks injected input, and a low-level hook sees that mark, but a hook
/// event and a dialog event are different events. The honest rule: an event is
/// physical only if a **non-injected** input event was observed within
/// [`PHYSICAL_CORRELATION_MS`] before it. A wrong "injected" costs the owner one
/// extra click; a wrong "physical" would let remote input approve. So when unsure,
/// it says injected.
#[derive(Default)]
pub struct PhysicalCorrelator {
    last_physical: Option<Mono>,
}

impl PhysicalCorrelator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Report what the hook saw since the last call (`PhysicalState::take_activity`).
    pub fn observe(&mut self, now: Mono, physical_activity_seen: bool) {
        if physical_activity_seen {
            self.last_physical = Some(now);
        }
    }

    /// Like [`Self::origin`], but a physical verdict **spends** the observation, so
    /// one real keypress or click can vouch for exactly one approval attempt and
    /// not for a burst of injected ones that follow it inside the window. Use this
    /// for Allow; plain `origin` is fine for anything that only denies.
    pub fn consume(&mut self, now: Mono) -> InputOrigin {
        let o = self.origin(now);
        if o == InputOrigin::Physical {
            self.last_physical = None;
        }
        o
    }

    pub fn origin(&self, now: Mono) -> InputOrigin {
        match self.last_physical {
            Some(t) if now.since(t) <= PHYSICAL_CORRELATION_MS => InputOrigin::Physical,
            _ => InputOrigin::Injected,
        }
    }

    /// Forget. Called when the dialog closes so a stale observation cannot carry
    /// over to the next one.
    pub fn reset(&mut self) {
        self.last_physical = None;
    }
}

/// Copy for pairing: names the *account*, not "Device connected".
pub fn pairing_copy(account: &str, phrase: &str) -> Vec<String> {
    vec![
        format!("Pair this computer with the Shiksha account of {account}?"),
        format!("Check that your browser shows the same phrase: \u{201c}{phrase}\u{201d}."),
        "Pairing lets that account ask to share this screen. It does not allow anyone to control it; that needs a separate approval each time.".to_string(),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prompt() -> ControlPrompt {
        ControlPrompt {
            controller_name: "Asha Rao".into(),
            controller_context: "Teacher".into(),
            meeting_title: "Maths revision".into(),
            device_name: "Library PC".into(),
            display_label: "Display 1".into(),
            capabilities: vec!["pointer.move".into(), "pointer.button".into(), "keyboard.text".into()],
            stop_shortcut: "Ctrl+Alt+Shift+F12".into(),
            recording: RecordingStatus::Known { recording: false },
            public_broadcast: false,
        }
    }

    fn open(t: u64) -> ConsentDialog {
        ConsentDialog::open(Mono(t), prompt(), false).unwrap()
    }

    const PHYS: InputOrigin = InputOrigin::Physical;
    const INJ: InputOrigin = InputOrigin::Injected;

    // ── T22: opens deny-by-default ──

    #[test]
    fn it_opens_with_deny_focused_and_allow_disabled() {
        let d = open(0);
        let v = d.view(Mono(0));
        assert_eq!(v.focus, Focus::Deny);
        assert!(!v.allow_enabled);
        assert_eq!(v.outcome, Outcome::Pending);
        assert_eq!(v.deny_label, "Deny");
    }

    #[test]
    fn allow_becomes_enabled_exactly_when_the_dwell_ends_and_the_label_counts_down() {
        let d = open(0);
        assert!(!d.allow_enabled(Mono(CONSENT_DWELL_MS - 1)));
        assert!(d.allow_enabled(Mono(CONSENT_DWELL_MS)));
        assert_eq!(d.view(Mono(0)).allow_label, "Allow (in 3s)");
        assert_eq!(d.view(Mono(2_001)).allow_label, "Allow (in 1s)");
        assert_eq!(d.view(Mono(2_999)).allow_label, "Allow (in 1s)", "never shows 0s while disabled");
        assert_eq!(d.view(Mono(3_000)).allow_label, "Allow");
    }

    #[test]
    fn enter_and_space_cannot_approve_before_the_dwell_and_can_only_deny() {
        for key in [KeyPress::Enter, KeyPress::Space] {
            let mut d = open(0);
            d.on_key(Mono(100), key, PHYS);
            assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Button), "{key:?} on the focused Deny denies");
        }
    }

    #[test]
    fn tab_cannot_reach_allow_before_the_dwell() {
        let mut d = open(0);
        d.on_key(Mono(1_000), KeyPress::Tab, PHYS);
        assert_eq!(d.view(Mono(1_000)).focus, Focus::Deny, "Allow is not focusable yet");
        d.on_key(Mono(1_100), KeyPress::Enter, PHYS);
        assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Button));
    }

    // ── approval needs dwell AND a physical hand ──

    #[test]
    fn a_physical_click_on_allow_after_the_dwell_approves() {
        // The positive twin of every refusal below.
        let mut d = open(0);
        d.on_click(Mono(CONSENT_DWELL_MS), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Allowed);
        let dec = d.decision().unwrap();
        assert_eq!(dec.origin, InputOrigin::Physical);
        assert_eq!(dec.capabilities, prompt().capabilities);
    }

    #[test]
    fn a_physical_click_before_the_dwell_is_ignored_and_counted() {
        let mut d = open(0);
        d.on_click(Mono(CONSENT_DWELL_MS - 1), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Pending);
        assert_eq!(d.refused_approvals(), 1);
        assert!(d.decision().is_none());
    }

    #[test]
    fn t13_an_injected_click_on_allow_never_approves_even_after_the_dwell() {
        let mut d = open(0);
        d.on_click(Mono(10_000), Control::Allow, INJ);
        assert_eq!(d.outcome(), Outcome::Pending, "remote input cannot answer the owner's dialog");
        assert_eq!(d.refused_approvals(), 1);
        assert!(d.decision().is_none());
        // And a real click afterwards still works: the dialog was not poisoned.
        d.on_click(Mono(10_100), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Allowed);
    }

    #[test]
    fn t13_an_injected_key_on_a_focused_allow_never_approves() {
        let mut d = open(0);
        d.on_key(Mono(CONSENT_DWELL_MS), KeyPress::Tab, PHYS); // focus Allow
        assert_eq!(d.view(Mono(CONSENT_DWELL_MS)).focus, Focus::Allow);
        d.on_key(Mono(CONSENT_DWELL_MS + 10), KeyPress::Space, INJ);
        assert_eq!(d.outcome(), Outcome::Pending);
        d.on_key(Mono(CONSENT_DWELL_MS + 20), KeyPress::Space, PHYS);
        assert_eq!(d.outcome(), Outcome::Allowed, "the physical twin approves");
    }

    #[test]
    fn keyboard_approval_works_only_through_tab_then_a_physical_enter() {
        let mut d = open(0);
        d.on_key(Mono(3_500), KeyPress::Tab, PHYS);
        d.on_key(Mono(3_600), KeyPress::Enter, PHYS);
        assert_eq!(d.outcome(), Outcome::Allowed);
    }

    // ── denying is always possible, and so is not blocked by provenance ──

    #[test]
    fn deny_escape_and_close_all_deny_from_any_origin() {
        for (name, act) in [
            ("deny", Box::new(|d: &mut ConsentDialog, o| d.on_click(Mono(10), Control::Deny, o)) as Box<dyn Fn(&mut ConsentDialog, InputOrigin)>),
            ("close", Box::new(|d, o| d.on_click(Mono(10), Control::Close, o))),
            ("escape", Box::new(|d, o| d.on_key(Mono(10), KeyPress::Escape, o))),
        ] {
            for o in [PHYS, INJ] {
                let mut d = open(0);
                act(&mut d, o);
                assert!(matches!(d.outcome(), Outcome::Denied(_)), "{name} {o:?}");
            }
        }
    }

    #[test]
    fn escape_denies_even_after_the_dwell_with_allow_focused() {
        let mut d = open(0);
        d.on_key(Mono(4_000), KeyPress::Tab, PHYS);
        d.on_key(Mono(4_100), KeyPress::Escape, PHYS);
        assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Escape));
    }

    // ── no auto-approve ──

    #[test]
    fn the_deadline_denies_and_never_approves() {
        let mut d = open(0);
        d.tick(Mono(APPROVAL_DEADLINE_MS - 1));
        assert_eq!(d.outcome(), Outcome::Pending);
        d.tick(Mono(APPROVAL_DEADLINE_MS));
        assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Expired));
        assert!(d.decision().is_none());
    }

    #[test]
    fn a_click_that_arrives_after_the_deadline_cannot_approve() {
        let mut d = open(0);
        d.on_click(Mono(APPROVAL_DEADLINE_MS + 1), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Expired));
    }

    #[test]
    fn nothing_changes_once_decided() {
        let mut d = open(0);
        d.on_click(Mono(10), Control::Deny, PHYS);
        d.on_click(Mono(5_000), Control::Allow, PHYS);
        d.on_key(Mono(5_100), KeyPress::Enter, PHYS);
        assert_eq!(d.outcome(), Outcome::Denied(DenyReason::Button));
        let mut a = open(0);
        a.on_click(Mono(5_000), Control::Allow, PHYS);
        a.on_click(Mono(5_100), Control::Deny, PHYS);
        assert_eq!(a.outcome(), Outcome::Allowed, "a decision is final");
    }

    // ── refusing to open ──

    #[test]
    fn it_will_not_open_while_a_grant_can_inject() {
        assert_eq!(ConsentDialog::open(Mono(0), prompt(), true).err(), Some(OpenRefusal::GrantActive));
        assert!(ConsentDialog::open(Mono(0), prompt(), false).is_ok(), "and does open when none can");
    }

    #[test]
    fn it_will_not_open_with_an_unknown_recording_policy_or_for_a_public_broadcast() {
        let mut p = prompt();
        p.recording = RecordingStatus::Unknown;
        assert_eq!(ConsentDialog::open(Mono(0), p, false).err(), Some(OpenRefusal::PolicyUnknown));
        let mut p = prompt();
        p.public_broadcast = true;
        assert_eq!(ConsentDialog::open(Mono(0), p, false).err(), Some(OpenRefusal::PublicBroadcast));
    }

    #[test]
    fn it_will_not_open_with_no_capabilities_or_no_verified_name() {
        let mut p = prompt();
        p.capabilities.clear();
        assert_eq!(ConsentDialog::open(Mono(0), p, false).err(), Some(OpenRefusal::NoCapabilities));
        let mut p = prompt();
        p.controller_name = "  ".into();
        assert_eq!(ConsentDialog::open(Mono(0), p, false).err(), Some(OpenRefusal::NoName));
    }

    // ── the copy ──

    #[test]
    fn the_copy_names_who_what_where_and_the_real_power() {
        let text = open(0).view(Mono(0)).lines.join("\n");
        for must in ["Asha Rao", "Teacher", "Library PC", "Display 1", "Maths revision", "Ctrl+Alt+Shift+F12"] {
            assert!(text.contains(must), "missing {must:?}");
        }
        assert!(text.contains("files and terminals"), "the real power, not a euphemism");
        assert!(text.contains(POWER_STATEMENT));
        assert!(text.contains("separate approval"), "files are separate consent");
        assert!(text.contains("not being recorded"));
    }

    #[test]
    fn the_copy_lists_exactly_the_granted_capabilities() {
        let text = open(0).view(Mono(0)).lines.join("\n");
        assert!(text.contains("move the pointer, click the mouse buttons, and type text"), "{text}");
        assert!(!text.contains("scroll"), "a capability not granted is not mentioned");
        assert!(!text.contains("press keys"), "keyboard.physical was not granted");
    }

    #[test]
    fn a_recording_session_says_so() {
        let mut p = prompt();
        p.recording = RecordingStatus::Known { recording: true };
        let d = ConsentDialog::open(Mono(0), p, false).unwrap();
        assert!(d.view(Mono(0)).lines.join("\n").contains("is being recorded"));
    }

    #[test]
    fn an_unknown_capability_is_shown_verbatim_never_dropped() {
        assert_eq!(capability_copy("shell"), "shell");
        assert_eq!(capability_copy("pointer.scroll"), "scroll");
    }

    #[test]
    fn the_title_uses_the_verified_name_and_does_not_say_remote_assistance() {
        let v = open(0).view(Mono(0));
        assert_eq!(v.title, "Allow Asha Rao to control this computer?");
        assert!(!v.lines.join(" ").to_lowercase().contains("remote assistance"));
    }

    #[test]
    fn the_view_never_offers_a_default_that_approves() {
        // No point in time makes Allow the initial focus.
        let d = open(0);
        for t in [0, 1_000, 3_000, 10_000, 29_999] {
            assert_eq!(d.view(Mono(t)).focus, Focus::Deny, "t={t}");
        }
    }

    #[test]
    fn pairing_copy_names_the_account_and_says_it_is_not_control() {
        let c = pairing_copy("Asha Rao", "blue otter").join("\n");
        assert!(c.contains("Asha Rao") && c.contains("blue otter"));
        assert!(c.contains("does not allow anyone to control"));
    }

    // ── physical correlation ──

    #[test]
    fn with_no_observed_hardware_input_everything_is_injected() {
        let c = PhysicalCorrelator::new();
        assert_eq!(c.origin(Mono(1_000)), INJ);
    }

    #[test]
    fn hardware_input_makes_the_next_half_second_physical_and_no_longer() {
        let mut c = PhysicalCorrelator::new();
        c.observe(Mono(1_000), true);
        assert_eq!(c.origin(Mono(1_000)), PHYS);
        assert_eq!(c.origin(Mono(1_000 + PHYSICAL_CORRELATION_MS)), PHYS);
        assert_eq!(c.origin(Mono(1_000 + PHYSICAL_CORRELATION_MS + 1)), INJ);
    }

    #[test]
    fn one_physical_event_vouches_for_one_approval_attempt_not_a_burst() {
        let mut c = PhysicalCorrelator::new();
        c.observe(Mono(1_000), true);
        assert_eq!(c.consume(Mono(1_010)), PHYS, "the real click");
        assert_eq!(c.consume(Mono(1_020)), INJ, "an injected one right behind it is not covered");
        assert_eq!(c.consume(Mono(1_030)), INJ);
        // A new real event vouches again.
        c.observe(Mono(1_040), true);
        assert_eq!(c.consume(Mono(1_050)), PHYS);
    }

    #[test]
    fn plain_origin_does_not_spend_the_observation() {
        let mut c = PhysicalCorrelator::new();
        c.observe(Mono(0), true);
        assert_eq!(c.origin(Mono(10)), PHYS);
        assert_eq!(c.origin(Mono(20)), PHYS);
    }

    // ── pairing / share confirmations use the same rules ──

    fn pairing(t: u64) -> ConsentDialog {
        ConsentDialog::open_confirmation(Mono(t), "Pair this computer?".into(), pairing_copy("Asha Rao", "blue otter"), false)
            .unwrap()
    }

    #[test]
    fn a_pairing_confirmation_shows_its_own_words_and_names_the_account() {
        let v = pairing(0).view(Mono(0));
        assert_eq!(v.title, "Pair this computer?");
        assert!(v.lines.join("\n").contains("Asha Rao"));
        assert!(!v.lines.join("\n").contains("files and terminals"), "it is not a control prompt");
    }

    #[test]
    fn a_pairing_confirmation_obeys_every_consent_rule() {
        let mut d = pairing(0);
        let v = d.view(Mono(0));
        assert_eq!((v.focus, v.allow_enabled), (Focus::Deny, false));
        d.on_click(Mono(100), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Pending, "dwell");
        d.on_click(Mono(5_000), Control::Allow, INJ);
        assert_eq!(d.outcome(), Outcome::Pending, "injected");
        d.on_click(Mono(5_100), Control::Allow, PHYS);
        assert_eq!(d.outcome(), Outcome::Allowed, "the physical twin");
        let mut e = pairing(0);
        e.tick(Mono(APPROVAL_DEADLINE_MS));
        assert_eq!(e.outcome(), Outcome::Denied(DenyReason::Expired), "deadline denies");
    }

    #[test]
    fn a_confirmation_will_not_open_under_a_live_grant_or_empty() {
        assert_eq!(
            ConsentDialog::open_confirmation(Mono(0), "t".into(), vec!["l".into()], true).err(),
            Some(OpenRefusal::GrantActive)
        );
        assert!(ConsentDialog::open_confirmation(Mono(0), "".into(), vec!["l".into()], false).is_err());
        assert!(ConsentDialog::open_confirmation(Mono(0), "t".into(), vec![], false).is_err());
    }

    #[test]
    fn observing_nothing_does_not_refresh_an_old_observation() {
        let mut c = PhysicalCorrelator::new();
        c.observe(Mono(0), true);
        c.observe(Mono(400), false);
        assert_eq!(c.origin(Mono(600)), INJ);
    }

    #[test]
    fn reset_forgets_so_a_stale_observation_cannot_carry_to_the_next_dialog() {
        let mut c = PhysicalCorrelator::new();
        c.observe(Mono(0), true);
        c.reset();
        assert_eq!(c.origin(Mono(10)), INJ);
    }

    #[test]
    fn an_injected_flood_with_no_hardware_input_can_never_approve_through_the_whole_stack() {
        // Correlator + dialog together: the property T13 asks for.
        let mut c = PhysicalCorrelator::new();
        let mut d = open(0);
        for t in (3_000..9_000).step_by(37) {
            c.observe(Mono(t), false); // nothing from real hardware
            d.on_click(Mono(t), Control::Allow, c.origin(Mono(t)));
            d.on_key(Mono(t), KeyPress::Tab, c.origin(Mono(t)));
            d.on_key(Mono(t), KeyPress::Enter, c.origin(Mono(t)));
        }
        assert_ne!(d.outcome(), Outcome::Allowed);
        assert!(d.refused_approvals() > 0, "the attempts were counted");
    }
}
