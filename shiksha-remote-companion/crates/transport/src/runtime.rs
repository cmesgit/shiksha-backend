//! Blocking workers that execute the core's driver state machines.
//!
//! All the *decisions* (when to poll, when never to retry, what a 409 means) are
//! in `shiksha_companion_core::{drivers, credentials}` and are type-checked and
//! unit-tested there. This module only performs the I/O they ask for and reports
//! results back. If you find yourself adding a retry rule here, it belongs in
//! core.
//!
//! NOT YET COMPILED (see HANDOFF). Threading model: two workers, each on its own
//! thread with its own HTTP client —
//!
//! * [`DeviceWorker`]: refresh and device heartbeat. Its HTTP timeout is long
//!   (tens of seconds) and it may block for that long.
//! * [`LeaseWorker`]: lease renewal only. Its timeout is
//!   [`LEASE_HTTP_TIMEOUT_MS`], so a stuck refresh or heartbeat can never delay
//!   a lease. Revocation latency is bounded by the lease; this thread is the
//!   reason it is.
//!
//! No lock is ever held across an HTTP call.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use shiksha_companion_core::adapter::InputAdapter;
use shiksha_companion_core::clock::{Entropy, Mono, MonotonicClock};
use shiksha_companion_core::companion::{InputOrigin, TransportEvent};
use shiksha_companion_core::credentials::{Credentials, Effect, RefreshOutcome};
use shiksha_companion_core::drivers::{
    activation_plan, classify_lease_error, ActivationFailure, FinalizeResult, FinalizeStep,
    HeartbeatSchedule, LeaseCallResult, LeaseDriver, PairingAction, PairingFlow, PairingState,
    PollResult, PollStep,
};
use shiksha_companion_core::secrets::{
    SecretStore, DEVICE_ID_NAME, REFRESH_INFLIGHT_NAME, REFRESH_NAME,
};
use shiksha_companion_core::ticket::KeySet;

use crate::device_api::{ApiError, DeviceApi};
use crate::http::{Http, HttpError};
use crate::jwt::{self, TrustRoot};
use crate::session::{Driver, Effects, NetEvent};

/// Where the runtime's outgoing packets and notices go. Implemented by the
/// LiveKit crate, which owns the publishing task.
pub trait EffectSink: Send {
    fn emit(&self, fx: Effects);
}

pub type SharedCreds = Arc<Mutex<Credentials>>;
pub type SharedDriver<A, C, E> = Arc<Mutex<Driver<A, C, E>>>;

fn lock_creds(c: &SharedCreds) -> MutexGuard<'_, Credentials> {
    // Credentials hold no authority to inject, so a poisoned lock is recoverable.
    c.lock().unwrap_or_else(|p| p.into_inner())
}

/// Lock the driver. A poisoned lock means a panic happened mid-decision and the
/// state may be half-updated, so close the gate before carrying on.
pub fn lock_driver<A: InputAdapter, C: MonotonicClock, E: Entropy>(
    d: &SharedDriver<A, C, E>,
) -> MutexGuard<'_, Driver<A, C, E>> {
    match d.lock() {
        Ok(g) => g,
        Err(p) => {
            let mut g = p.into_inner();
            let _ = g.companion_mut().local_stop();
            g
        }
    }
}

// ── Pairing ───────────────────────────────────────────────────────────────

/// What the owner sees and does. The real implementation is the native consent
/// UI (RC-014); until then the app has a console stand-in.
pub trait PairingUi {
    /// Show the code the owner types into their signed-in browser.
    fn show_code(&mut self, user_code: &str);
    /// Ask whether `account` and `phrase` match what the browser shows. The
    /// returned origin must be `Physical` for the answer to count.
    fn ask_owner(&mut self, account: &str, phrase: &str) -> (InputOrigin, bool);
    fn tick(&mut self) {}
}

#[derive(Debug, PartialEq, Eq)]
pub enum PairError {
    Create(String),
    Abandoned(shiksha_companion_core::drivers::AbandonReason),
    /// The flow ended "paired" without producing credentials (should not happen).
    NotSaved,
}

fn poll_result(r: Result<crate::device_api::PairingPoll, ApiError>) -> PollResult {
    match r {
        Ok(p) => match (p.account_display_name, p.verification_phrase) {
            (Some(account), Some(phrase)) if p.state == "browser_confirmed" => {
                PollResult::Confirmed { account, phrase }
            }
            _ => PollResult::Waiting,
        },
        Err(ApiError::Server { status: 404 | 410, .. }) => PollResult::Gone,
        Err(ApiError::Server { status: 429, .. }) => PollResult::RateLimited,
        Err(_) => PollResult::Transient,
    }
}

fn finalize_result(r: Result<crate::device_api::PairedDevice, ApiError>) -> FinalizeResult {
    match r {
        Ok(d) => FinalizeResult::Paired {
            device_id: d.device_id,
            access_token: d.access_token,
            refresh_token: d.refresh_token,
            expires_in_s: d.expires_in_s,
        },
        Err(ApiError::Http(HttpError::NotSent(_))) => FinalizeResult::NotSent,
        // Redemption is one-time. Anything that might have reached the server
        // and not produced tokens we can read is uncertain, never retried.
        Err(ApiError::Http(HttpError::Ambiguous(_))) | Err(ApiError::BadBody(_)) => FinalizeResult::Uncertain,
        Err(ApiError::Server { status, code, .. }) if (400..500).contains(&status) => {
            FinalizeResult::Rejected { code }
        }
        Err(_) => FinalizeResult::Uncertain,
    }
}

/// Run a whole pairing, blocking. Returns credentials that have already been
/// saved (or `NotSaved`).
pub fn pair<H: Http>(
    api: &mut DeviceApi<H>,
    clock: &dyn MonotonicClock,
    ui: &mut dyn PairingUi,
    store: &dyn SecretStore,
    device_name: &str,
    platform: &str,
    arch: &str,
    app_version: &str,
) -> Result<Credentials, PairError> {
    let mut flow = PairingFlow::new();
    let sent = clock.now();
    let started = api
        .create_pairing(device_name, platform, arch, app_version)
        .map_err(|e| PairError::Create(format!("{e:?}")))?;
    // The secret is a credential: it lives in `flow`, nowhere else, and is wiped
    // by the flow when the pairing ends.
    for a in flow.on_started(sent, started.pairing_id, started.device_secret, &started.user_code, started.poll_interval_s)
    {
        if let PairingAction::ShowCode(code) = a {
            ui.show_code(&code);
        }
    }

    let mut creds: Option<Credentials> = None;
    let mut persist_failed = false;
    // The question being asked, kept so a refused (non-physical) answer is
    // followed by asking again rather than by waiting forever.
    let mut asking: Option<(String, String)> = None;
    loop {
        ui.tick();
        let now = clock.now();
        for a in flow.check_expiry(now) {
            if let PairingAction::Abandoned(w) = a {
                return Err(PairError::Abandoned(w));
            }
        }
        match flow.state() {
            PairingState::Abandoned(why) => return Err(PairError::Abandoned(why)),
            PairingState::Paired => break,
            _ => {}
        }

        match flow.begin_poll(now) {
            PollStep::Poll(req) => {
                let r = poll_result(api.poll_pairing(&req.pairing_id, &req.secret));
                for a in flow.on_poll(clock.now(), r) {
                    if let PairingAction::AskOwner { account, phrase } = a {
                        asking = Some((account, phrase));
                    }
                }
            }
            PollStep::Expired => {
                return Err(PairError::Abandoned(shiksha_companion_core::drivers::AbandonReason::Expired))
            }
            PollStep::NotDue => {}
        }

        if flow.state() == PairingState::AwaitingOwner {
            if let Some((account, phrase)) = asking.clone() {
                let (origin, allow) = ui.ask_owner(&account, &phrase);
                match flow.owner_decision(origin, allow) {
                    Ok(actions) => {
                        asking = None;
                        for a in actions {
                            if let PairingAction::Abandoned(w) = a {
                                return Err(PairError::Abandoned(w));
                            }
                        }
                    }
                    // Injected input: refused, state unchanged, ask again.
                    Err(_) => {}
                }
            }
        }

        if let FinalizeStep::Finalize(req) = flow.begin_finalize(clock.now()) {
            let r = finalize_result(api.finalize_pairing(&req.pairing_id, &req.secret));
            for a in flow.on_finalize(r) {
                match a {
                    PairingAction::Persist { device_id, refresh_token } => {
                        // Save before anything else uses the credentials.
                        let ok = store.put(REFRESH_NAME, refresh_token.as_bytes()).is_ok()
                            && store.put(DEVICE_ID_NAME, device_id.as_bytes()).is_ok();
                        persist_failed = !ok;
                    }
                    PairingAction::Paired(c) => creds = Some(*c),
                    PairingAction::Abandoned(w) => return Err(PairError::Abandoned(w)),
                    _ => {}
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }
    let mut c = creds.ok_or(PairError::NotSaved)?;
    if persist_failed {
        // The server holds a credential we lost. The session works until the
        // access token expires; the caller sees StoreFailed and must re-pair.
        c.mark_store_failed();
    }
    Ok(c)
}

// ── Device worker: refresh + heartbeat ────────────────────────────────────

pub struct DeviceWorker<H: Http> {
    pub api: DeviceApi<H>,
    pub creds: SharedCreds,
    pub store: Arc<dyn SecretStore + Send + Sync>,
    pub clock: Box<dyn MonotonicClock + Send>,
    pub heartbeat: HeartbeatSchedule,
    pub app_version: String,
    pub protocol: (u64, u64),
    /// Set by the caller while a screen share is live.
    pub sharing: Arc<AtomicBool>,
    /// Mirrors the server's kill switch. When set, the caller must stop sharing
    /// and refuse new requests.
    pub disabled: Arc<AtomicBool>,
}

impl<H: Http> DeviceWorker<H> {
    /// One pass: refresh if due, then heartbeat if due. Blocking.
    pub fn step(&mut self) {
        self.refresh_if_due();
        self.heartbeat_if_due();
    }

    fn refresh_if_due(&mut self) {
        let now = self.clock.now();
        let attempt = match lock_creds(&self.creds).begin_refresh(now) {
            Some(a) => a,
            None => return,
        };

        // Write the in-flight marker BEFORE sending. If we cannot, do not send:
        // a crash mid-refresh would then be unrecognisable at the next start,
        // and a spent token would look like a good one.
        if self.store.put(REFRESH_INFLIGHT_NAME, b"1").is_err() {
            let now = self.clock.now();
            lock_creds(&self.creds).on_refresh(&attempt, RefreshOutcome::NotSent, now);
            return;
        }

        let outcome = self.api.refresh(&attempt);
        let definitive = !matches!(outcome, RefreshOutcome::Ambiguous);
        let now = self.clock.now();
        let effects = lock_creds(&self.creds).on_refresh(&attempt, outcome, now);

        for e in effects {
            match e {
                Effect::PersistRefresh(token) => {
                    let ok = self.store.put(REFRESH_NAME, token.as_bytes()).is_ok();
                    lock_creds(&self.creds).on_persisted(ok);
                }
                Effect::ForgetRefresh => {
                    let _ = self.store.delete(REFRESH_NAME);
                }
            }
        }
        // Keep the marker after an ambiguous outcome: the request may still be
        // running, and a crash now must be treated as "may have rotated".
        if definitive {
            let _ = self.store.delete(REFRESH_INFLIGHT_NAME);
        }
    }

    fn heartbeat_if_due(&mut self) {
        let now = self.clock.now();
        if !self.heartbeat.due(now) {
            return;
        }
        let token = match lock_creds(&self.creds).access(now) {
            Some(t) => t.to_string(),
            None => return, // nothing to authenticate with yet; refresh will fix it
        };
        let sharing = self.sharing.load(Ordering::SeqCst);
        let r = self.api.heartbeat(&token, &self.app_version, self.protocol.0, self.protocol.1, sharing);
        let now = self.clock.now();
        match r {
            Ok(hb) => {
                self.disabled.store(hb.disabled, Ordering::SeqCst);
                self.heartbeat.on_success(now, hb.disabled);
            }
            Err(ApiError::Server { status: 401, ref code, .. })
                if matches!(code.as_str(), "device_token_stale" | "device_auth_required" | "device_token_invalid") =>
            {
                lock_creds(&self.creds).on_access_rejected();
                self.heartbeat.on_failure(now);
            }
            Err(_) => self.heartbeat.on_failure(now),
        }
    }
}

// ── Lease worker ──────────────────────────────────────────────────────────

pub struct LeaseWorker<H: Http, A: InputAdapter, C: MonotonicClock, E: Entropy> {
    pub api: DeviceApi<H>,
    pub creds: SharedCreds,
    pub driver: SharedDriver<A, C, E>,
    pub clock: Box<dyn MonotonicClock + Send>,
    pub schedule: LeaseDriver,
    pub trust: Arc<TrustRoot>,
    pub sink: Box<dyn EffectSink>,
    /// The grant being renewed. Cleared when authority ends.
    pub grant_id: Option<String>,
}

impl<H: Http, A: InputAdapter, C: MonotonicClock, E: Entropy> LeaseWorker<H, A, C, E> {
    /// One renewal attempt if one is due. Blocking, bounded by the HTTP timeout.
    pub fn step(&mut self) {
        let now = self.clock.now();
        let Some(grant_id) = self.grant_id.clone() else { return };
        if !self.schedule.due(now) {
            return;
        }
        // No authority to renew once the companion has ended the grant.
        let req = match lock_driver(&self.driver).companion_mut().begin_lease() {
            Some(r) => r,
            None => {
                self.grant_id = None;
                return;
            }
        };
        let Some(token) = lock_creds(&self.creds).access(now).map(str::to_string) else {
            // Without an access token there is nothing to send; the deadline is
            // already running and will end the grant if this persists.
            return;
        };

        self.schedule.begin(now);
        let result = self.api.lease(&token, &grant_id, &req.epoch, req.expected_version, &req.nonce);
        self.schedule.finish();

        match self.interpret(result) {
            LeaseCallResult::Lease(jwt_str) => self.apply(&jwt_str),
            LeaseCallResult::TokenRejected => lock_creds(&self.creds).on_access_rejected(),
            LeaseCallResult::AuthorityGone(_) => {
                let fx = lock_driver(&self.driver).handle(NetEvent::Transport(TransportEvent::ServerInactive));
                self.grant_id = None;
                self.sink.emit(fx);
            }
            LeaseCallResult::Transient => {}
        }
    }

    fn interpret(&self, r: Result<String, ApiError>) -> LeaseCallResult {
        match r {
            Ok(j) => LeaseCallResult::Lease(j),
            Err(ApiError::Server { status, code, .. }) => classify_lease_error(status, &code),
            Err(_) => LeaseCallResult::Transient,
        }
    }

    fn apply(&mut self, lease_jwt: &str) {
        let Ok(v) = jwt::verify(lease_jwt, &self.trust) else { return };
        let Ok(claims) = jwt::lease_claims(&v.payload) else { return };
        let keys = KeySet::new(self.trust.kids());
        let mut d = lock_driver(&self.driver);
        // A rejected lease simply does not extend anything; the deadline stands.
        if let Ok(outs) = d.companion_mut().on_lease(&v.header, &keys, &claims) {
            drop(d);
            self.sink.emit(crate::session::lower(outs));
        }
    }
}

// ── Activation ────────────────────────────────────────────────────────────

#[derive(Debug, PartialEq, Eq)]
pub enum ActivateError {
    NotPhysical,
    /// Nothing is active. `retry_same_consent` says whether the owner's approval
    /// may be reused for another attempt.
    Failed { why: ActivationFailure, retry_same_consent: bool, terminated: bool },
}

pub struct ActivateParams<'a> {
    pub access_token: &'a str,
    pub grant_id: &'a str,
    pub consent_nonce: &'a str,
    pub expected_version: u64,
    pub capture_generation: u64,
    pub approved_capabilities: Vec<String>,
    pub wall_now_unix: i64,
}

/// Activate a grant the owner approved with a physical click. Returns the status
/// packets to send (ready + first challenge). On any failure
/// after the server may believe the grant is active, ends it with
/// `protocol_error`; the plan for which failures need that is in core.
pub fn activate_grant<H, A, C, E>(
    api: &mut DeviceApi<H>,
    driver: &SharedDriver<A, C, E>,
    clock: &dyn MonotonicClock,
    trust: &TrustRoot,
    origin: InputOrigin,
    p: ActivateParams<'_>,
) -> Result<Effects, ActivateError>
where
    H: Http,
    A: InputAdapter,
    C: MonotonicClock,
    E: Entropy,
{
    if origin != InputOrigin::Physical {
        return Err(ActivateError::NotPhysical);
    }
    // Both before the request leaves: the nonce rides in `lease_nonce`, and the
    // first lease's deadline runs from `sent`.
    let nonce = lock_driver(driver).companion_mut().fresh_nonce();
    let sent: Mono = clock.now();

    let fail = |api: &mut DeviceApi<H>, why: ActivationFailure| -> ActivateError {
        let plan = activation_plan(why);
        let terminated = plan.terminate
            && api.terminate(p.access_token, p.grant_id, "protocol_error", p.expected_version).is_ok();
        ActivateError::Failed { why, retry_same_consent: plan.retry_same_consent, terminated }
    };

    let act = match api.activate(
        p.access_token,
        p.grant_id,
        p.consent_nonce,
        p.expected_version,
        p.capture_generation,
        &nonce,
    ) {
        Ok(a) => a,
        Err(ApiError::Http(HttpError::NotSent(_))) | Err(ApiError::Build(_)) => {
            return Err(fail(api, ActivationFailure::NotSent))
        }
        Err(ApiError::Server { .. }) => return Err(fail(api, ActivationFailure::ServerRefused)),
        Err(_) => return Err(fail(api, ActivationFailure::Ambiguous)),
    };

    // From here the server believes the grant is active.
    let verified = match jwt::verify(&act.activation_jwt, trust)
        .ok()
        .and_then(|v| jwt::activation_claims(&v.payload).ok().map(|c| (v.header, v.token_hash, c)))
    {
        Some(x) => x,
        None => return Err(fail(api, ActivationFailure::ReplyInvalid)),
    };
    let (header, hash, claims) = verified;
    let keys = KeySet::new(trust.kids());

    let outs = {
        let mut d = lock_driver(driver);
        let c = d.companion_mut();
        if c.activate(
            origin,
            &header,
            &keys,
            claims,
            hash,
            p.approved_capabilities.clone(),
            p.wall_now_unix,
            sent,
        )
        .is_err()
            || !c.adopt_activation_lease(nonce, sent)
        {
            drop(d);
            return Err(fail(api, ActivationFailure::ReplyInvalid));
        }
        let lease = jwt::verify(&act.lease_jwt, trust)
            .ok()
            .and_then(|v| jwt::lease_claims(&v.payload).ok().map(|c| (v.header, c)));
        match lease {
            Some((h, lc)) => match c.on_lease(&h, &keys, &lc) {
                Ok(outs) => outs,
                Err(_) => {
                    // We activated locally but the lease is bad: end it here too.
                    let _ = c.local_stop();
                    drop(d);
                    return Err(fail(api, ActivationFailure::ReplyInvalid));
                }
            },
            None => {
                let _ = c.local_stop();
                drop(d);
                return Err(fail(api, ActivationFailure::ReplyInvalid));
            }
        }
    };
    // `ready` and the first challenge. The caller must emit these: without them
    // the controller never learns it may start.
    Ok(crate::session::lower(outs))
}

/// The server's `code` for a failed call, if it sent one.
pub fn error_code(e: &ApiError) -> Option<&str> {
    match e {
        ApiError::Server { code, .. } => Some(code.as_str()),
        _ => None,
    }
}
