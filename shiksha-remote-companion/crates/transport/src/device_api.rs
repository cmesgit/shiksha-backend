//! The device plane of `/api/sessions/collaboration/v1/`.
//!
//! Written against the *server code*, not only `api-contracts.md`, because the
//! two disagree in places (recorded in `evidence/companion-core.md`):
//! * `device-token/` requires `device_id` in the body; the contract omits it;
//! * the device routes do not use `Idempotency-Key` (the contract says all
//!   non-heartbeat mutations do), and `lease/` must not: a replayed lease is one
//!   that outlives its deadline;
//! * the server holds no DPoP nonce, though the architecture text mentions one.
//!
//! Auth schemes (`auth`/`devices.py`): resource routes send
//! `Authorization: DPoP <access_token>` plus a `DPoP: <proof with ath>`;
//! pairing and refresh send a proof with **no** `ath`; pairing poll and finalize
//! send `Authorization: Pairing <secret>` (never in a URL or body).

use serde_json::{json, Value};
use shiksha_companion_core::clock::Entropy;
use shiksha_companion_core::credentials::{RefreshAttempt, RefreshOutcome};
use shiksha_companion_core::dpop::{
    build_proof, htu_for, ClockOffset, DpopError, Hasher, ProofRequest, ProofSigner,
};

use crate::http::{Http, HttpError, HttpRequest, HttpResponse};

/// Credentials the server definitively refuses for a refresh. Anything else that
/// is not a success is ambiguous, not fatal, because erasing a good credential
/// over a transient fault would force a re-pair.
pub const DEAD_CODES: [&str; 4] =
    ["credential_invalid", "credential_expired", "device_revoked", "device_unknown"];

#[derive(Debug, PartialEq, Eq)]
pub enum ApiError {
    /// Could not build a request (bad URL, signing failure). Nothing was sent.
    Build(String),
    Http(HttpError),
    /// A non-2xx answer in the contract's `{code, detail, request_id}` envelope.
    Server { status: u16, code: String, detail: String, retry_after_s: Option<u64> },
    /// A 2xx whose body was not what the contract promises. The request
    /// **succeeded** server-side; do not blindly repeat a mutation.
    BadBody(&'static str),
}

pub struct ApiConfig {
    /// `COLLABORATION_CANONICAL_URL` including the route prefix, e.g.
    /// `https://api.shikshacom.com/api/sessions/collaboration/v1`. Must match the
    /// server's setting exactly: it is both the request URL and the proof's `htu`.
    pub canonical_base: String,
}

/// The device key, hasher, entropy and learned clock offset, bundled.
pub struct Proofs {
    pub signer: Box<dyn ProofSigner + Send>,
    pub hasher: Box<dyn Hasher + Send>,
    pub entropy: Box<dyn Entropy + Send>,
    pub offset: ClockOffset,
}

pub enum Auth<'a> {
    /// Pairing creation and credential refresh: a proof, no `ath`, no header.
    ProofOnly,
    /// Pairing poll: the secret alone. The server does not read a proof here.
    PairingSecret(&'a str),
    /// Pairing finalize: the secret *and* a proof from the original key.
    PairingSecretAndProof(&'a str),
    /// Every resource route.
    Access(&'a str),
}

pub struct DeviceApi<H: Http> {
    cfg: ApiConfig,
    http: H,
    proofs: Proofs,
    /// Local wall-clock Unix seconds. Injected so tests can control it.
    wall: Box<dyn Fn() -> i64 + Send>,
}

fn error_body(resp: &HttpResponse) -> (String, String) {
    match serde_json::from_slice::<Value>(&resp.body) {
        Ok(v) => (
            v.get("code").and_then(Value::as_str).unwrap_or("").to_string(),
            v.get("detail").and_then(Value::as_str).unwrap_or("").to_string(),
        ),
        Err(_) => (String::new(), String::new()),
    }
}

fn string(v: &Value, k: &'static str) -> Result<String, ApiError> {
    v.get(k).and_then(Value::as_str).map(str::to_owned).ok_or(ApiError::BadBody(k))
}

fn uint(v: &Value, k: &'static str) -> Result<u64, ApiError> {
    v.get(k).and_then(Value::as_u64).ok_or(ApiError::BadBody(k))
}

impl<H: Http> DeviceApi<H> {
    pub fn new(cfg: ApiConfig, http: H, proofs: Proofs, wall: Box<dyn Fn() -> i64 + Send>) -> Self {
        Self { cfg, http, proofs, wall }
    }

    pub fn clock_offset(&self) -> ClockOffset {
        self.proofs.offset
    }

    /// Build a fully signed request without sending it.
    pub fn build(
        &mut self,
        method: &'static str,
        path: &str,
        body: Option<Value>,
        auth: Auth<'_>,
    ) -> Result<HttpRequest, ApiError> {
        let url = htu_for(&self.cfg.canonical_base, path).map_err(|e| ApiError::Build(format!("{e:?}")))?;
        let mut headers = vec![
            ("Accept".to_string(), "application/json".to_string()),
            ("Content-Type".to_string(), "application/json".to_string()),
        ];
        let (access, want_proof) = match auth {
            Auth::ProofOnly => (None, true),
            Auth::PairingSecret(s) => {
                headers.push(("Authorization".into(), format!("Pairing {s}")));
                (None, false)
            }
            Auth::PairingSecretAndProof(s) => {
                headers.push(("Authorization".into(), format!("Pairing {s}")));
                (None, true)
            }
            Auth::Access(t) => {
                headers.push(("Authorization".into(), format!("DPoP {t}")));
                (Some(t), true)
            }
        };
        if want_proof {
            let iat = self.proofs.offset.apply((self.wall)());
            let proof = build_proof(
                self.proofs.signer.as_ref(),
                self.proofs.hasher.as_ref(),
                self.proofs.entropy.as_mut(),
                &ProofRequest { method, htu: &url, iat, access_token: access },
            )
            .map_err(|e: DpopError| ApiError::Build(format!("{e:?}")))?;
            headers.push(("DPoP".into(), proof));
        }
        Ok(HttpRequest {
            method,
            url,
            headers,
            body: body.map(|b| b.to_string()),
        })
    }

    /// Send, and learn the server's clock from every reply so the *next* proof
    /// is right even when this one was refused for skew.
    fn send(&mut self, req: &HttpRequest) -> Result<HttpResponse, ApiError> {
        let resp = self.http.send(req).map_err(ApiError::Http)?;
        if let Some(server) = resp.date_unix {
            self.proofs.offset = ClockOffset::learn(server, (self.wall)());
        }
        Ok(resp)
    }

    /// Send and require a 2xx JSON object.
    fn call(
        &mut self,
        method: &'static str,
        path: &str,
        body: Option<Value>,
        auth: Auth<'_>,
    ) -> Result<Value, ApiError> {
        let req = self.build(method, path, body, auth)?;
        let resp = self.send(&req)?;
        if (200..300).contains(&resp.status) {
            if resp.status == 204 {
                return Ok(Value::Null);
            }
            return match serde_json::from_slice::<Value>(&resp.body) {
                Ok(v) if v.is_object() => Ok(v),
                _ => Err(ApiError::BadBody("not_an_object")),
            };
        }
        let (code, detail) = error_body(&resp);
        Err(ApiError::Server { status: resp.status, code, detail, retry_after_s: resp.retry_after_s })
    }

    // ── Pairing ──────────────────────────────────────────────────────────

    pub fn create_pairing(
        &mut self,
        name: &str,
        platform: &str,
        arch: &str,
        app_version: &str,
    ) -> Result<PairingStarted, ApiError> {
        let (x, y) = self.proofs.signer.public_xy();
        let jwk: Value = serde_json::from_str(&shiksha_companion_core::dpop::jwk_json(&x, &y))
            .map_err(|e| ApiError::Build(e.to_string()))?;
        let v = self.call(
            "POST",
            "pairings/",
            Some(json!({
                "name": name, "platform": platform, "arch": arch,
                "app_version": app_version, "public_jwk": jwk
            })),
            Auth::ProofOnly,
        )?;
        Ok(PairingStarted {
            pairing_id: string(&v, "pairing_id")?,
            device_secret: string(&v, "device_secret")?,
            user_code: string(&v, "user_code")?,
            expires_at: string(&v, "expires_at")?,
            poll_interval_s: uint(&v, "poll_interval_s")?,
        })
    }

    pub fn poll_pairing(&mut self, pairing_id: &str, secret: &str) -> Result<PairingPoll, ApiError> {
        let v = self.call(
            "POST",
            &format!("pairings/{pairing_id}/poll/"),
            Some(json!({})),
            Auth::PairingSecret(secret),
        )?;
        Ok(PairingPoll {
            state: string(&v, "state")?,
            account_display_name: v.get("account_display_name").and_then(Value::as_str).map(str::to_owned),
            verification_phrase: v.get("verification_phrase").and_then(Value::as_str).map(str::to_owned),
        })
    }

    /// Call only after the owner physically confirmed on this machine.
    pub fn finalize_pairing(&mut self, pairing_id: &str, secret: &str) -> Result<PairedDevice, ApiError> {
        let v = self.call(
            "POST",
            &format!("pairings/{pairing_id}/finalize/"),
            Some(json!({"local_confirm": true})),
            Auth::PairingSecretAndProof(secret),
        )?;
        Ok(PairedDevice {
            device_id: string(&v, "device_id")?,
            access_token: string(&v, "access_token")?,
            refresh_token: string(&v, "refresh_token")?,
            expires_in_s: uint(&v, "expires_in")?,
        })
    }

    // ── Credential refresh ───────────────────────────────────────────────

    /// One refresh request, classified for [`Credentials::on_refresh`]. Never
    /// retries internally: whether to retry is the credential layer's call.
    pub fn refresh(&mut self, attempt: &RefreshAttempt) -> RefreshOutcome {
        let req = match self.build(
            "POST",
            "device-token/",
            // `device_id` is required by the server although the contract omits it.
            Some(json!({"refresh_token": attempt.refresh_token, "device_id": attempt.device_id})),
            Auth::ProofOnly,
        ) {
            Ok(r) => r,
            // Failing to even build a request means nothing was sent.
            Err(_) => return RefreshOutcome::NotSent,
        };
        classify_refresh(self.send(&req))
    }

    // ── Everything below carries an access token ─────────────────────────

    pub fn heartbeat(
        &mut self,
        access: &str,
        app_version: &str,
        protocol_min: u64,
        protocol_max: u64,
        sharing: bool,
    ) -> Result<Heartbeat, ApiError> {
        let v = self.call(
            "POST",
            "device-heartbeat/",
            Some(json!({
                "app_version": app_version, "protocol_min": protocol_min,
                "protocol_max": protocol_max,
                "status": if sharing { "sharing" } else { "idle" }
            })),
            Auth::Access(access),
        )?;
        Ok(Heartbeat {
            server_time: string(&v, "server_time")?,
            minimum_version: v.get("minimum_version").and_then(Value::as_str).unwrap_or("").to_string(),
            disabled: v.get("disabled").and_then(Value::as_bool).unwrap_or(false),
        })
    }

    /// The owner picked a display locally; returns the join token.
    pub fn share(&mut self, access: &str, host_binding_id: &str, display: Value) -> Result<ShareToken, ApiError> {
        let v = self.call(
            "POST",
            &format!("host-bindings/{host_binding_id}/share/"),
            Some(json!({"display": display, "local_confirm": true})),
            Auth::Access(access),
        )?;
        Ok(ShareToken {
            livekit_url: string(&v, "livekit_url")?,
            token: string(&v, "token")?,
            identity: string(&v, "identity")?,
            join_expires_in_s: uint(&v, "join_expires_in")?,
        })
    }

    pub fn host_ready(
        &mut self,
        access: &str,
        host_binding_id: &str,
        participant_sid: &str,
        track_sid: &str,
        capture_generation: u64,
    ) -> Result<Value, ApiError> {
        self.call(
            "POST",
            &format!("host-bindings/{host_binding_id}/ready/"),
            Some(json!({
                "participant_sid": participant_sid, "track_sid": track_sid,
                "capture_generation": capture_generation
            })),
            Auth::Access(access),
        )
    }

    pub fn host_heartbeat(
        &mut self,
        access: &str,
        host_binding_id: &str,
        participant_sid: &str,
        track_sid: &str,
        capture_generation: u64,
    ) -> Result<Value, ApiError> {
        self.call(
            "POST",
            &format!("host-bindings/{host_binding_id}/heartbeat/"),
            Some(json!({
                "participant_sid": participant_sid, "track_sid": track_sid,
                "capture_generation": capture_generation
            })),
            Auth::Access(access),
        )
    }

    /// Activate after the owner physically approved. `lease_nonce` must come from
    /// `Companion::fresh_nonce` and `Companion::adopt_activation_lease` must be
    /// given the same nonce and the monotonic time this request was sent.
    pub fn activate(
        &mut self,
        access: &str,
        grant_id: &str,
        consent_nonce: &str,
        expected_version: u64,
        capture_generation: u64,
        lease_nonce: &str,
    ) -> Result<Activation, ApiError> {
        let v = self.call(
            "POST",
            &format!("grants/{grant_id}/activate/"),
            Some(json!({
                "consent_nonce": consent_nonce, "expected_version": expected_version,
                "capture_generation": capture_generation, "local_confirm": true,
                "lease_nonce": lease_nonce
            })),
            Auth::Access(access),
        )?;
        Ok(Activation {
            activation_jwt: string(&v, "activation_jwt")?,
            lease_jwt: string(&v, "lease_jwt")?,
        })
    }

    /// Renew. 🔴 No idempotency key, no retry-by-replay: see module docs.
    pub fn lease(
        &mut self,
        access: &str,
        grant_id: &str,
        epoch: &str,
        expected_version: u64,
        nonce: &str,
    ) -> Result<String, ApiError> {
        let v = self.call(
            "POST",
            &format!("grants/{grant_id}/lease/"),
            Some(json!({"epoch": epoch, "expected_version": expected_version, "nonce": nonce})),
            Auth::Access(access),
        )?;
        string(&v, "lease_jwt")
    }

    pub fn terminate(
        &mut self,
        access: &str,
        grant_id: &str,
        reason: &str,
        expected_version: u64,
    ) -> Result<Value, ApiError> {
        self.call(
            "POST",
            &format!("grants/{grant_id}/terminate/"),
            Some(json!({"reason": reason, "expected_version": expected_version})),
            Auth::Access(access),
        )
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct PairingStarted {
    pub pairing_id: String,
    /// A credential. Hold in memory only; never log.
    pub device_secret: String,
    /// What the owner types into the browser.
    pub user_code: String,
    pub expires_at: String,
    pub poll_interval_s: u64,
}

#[derive(Debug, PartialEq, Eq)]
pub struct PairingPoll {
    pub state: String,
    pub account_display_name: Option<String>,
    pub verification_phrase: Option<String>,
}

#[derive(PartialEq, Eq)]
pub struct PairedDevice {
    pub device_id: String,
    pub access_token: String,
    pub refresh_token: String,
    pub expires_in_s: u64,
}

impl std::fmt::Debug for PairedDevice {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PairedDevice").field("device_id", &self.device_id).finish_non_exhaustive()
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct Heartbeat {
    pub server_time: String,
    pub minimum_version: String,
    /// Server kill switch. A companion that sees this must stop sharing.
    pub disabled: bool,
}

#[derive(PartialEq, Eq)]
pub struct ShareToken {
    pub livekit_url: String,
    pub token: String,
    pub identity: String,
    pub join_expires_in_s: u64,
}

impl std::fmt::Debug for ShareToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ShareToken").field("identity", &self.identity).finish_non_exhaustive()
    }
}

#[derive(PartialEq, Eq)]
pub struct Activation {
    pub activation_jwt: String,
    pub lease_jwt: String,
}

impl std::fmt::Debug for Activation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Activation(<redacted>)")
    }
}

/// Turn a refresh response into what the credential layer needs.
///
/// Conservative by design: only outcomes that are *provably* "nothing rotated"
/// or *provably* "dead" get a precise answer. A 2xx we cannot parse is
/// `Ambiguous`, not an error — the server rotated, and we lost the new token.
pub fn classify_refresh(r: Result<HttpResponse, ApiError>) -> RefreshOutcome {
    let resp = match r {
        Ok(r) => r,
        Err(ApiError::Http(HttpError::NotSent(_))) => return RefreshOutcome::NotSent,
        Err(_) => return RefreshOutcome::Ambiguous,
    };
    match resp.status {
        200 => match serde_json::from_slice::<Value>(&resp.body) {
            Ok(v) => match (
                v.get("access_token").and_then(Value::as_str),
                v.get("refresh_token").and_then(Value::as_str),
                v.get("expires_in").and_then(Value::as_u64),
            ) {
                (Some(a), Some(r), Some(e)) if !a.is_empty() && !r.is_empty() => {
                    RefreshOutcome::Rotated {
                        access_token: a.to_string(),
                        refresh_token: r.to_string(),
                        expires_in_s: e,
                    }
                }
                _ => RefreshOutcome::Ambiguous,
            },
            Err(_) => RefreshOutcome::Ambiguous,
        },
        429 => RefreshOutcome::RateLimited { retry_after_s: resp.retry_after_s.unwrap_or(30) },
        401 => {
            let (code, _) = error_body(&resp);
            if code == "device_proof_invalid" {
                RefreshOutcome::ProofRejected
            } else if DEAD_CODES.contains(&code.as_str()) {
                RefreshOutcome::Rejected { code }
            } else {
                RefreshOutcome::Ambiguous
            }
        }
        _ => RefreshOutcome::Ambiguous,
    }
}
