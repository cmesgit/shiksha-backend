//! The device API against a scripted fake HTTP layer and a real P-256 key.
//!
//! Not yet compiled or run (see HANDOFF, RC-012). Inspects the signed proofs by
//! decoding them, so it checks what the server will actually see.

use std::sync::{Arc, Mutex};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::Value;
use shiksha_companion_core::clock::{CountingEntropy, Mono};
use shiksha_companion_core::credentials::{Credentials, RefreshAttempt, RefreshOutcome};
use shiksha_companion_core::dpop::{thumbprint, ClockOffset};
use shiksha_companion_transport::device_api::*;
use shiksha_companion_transport::http::{Http, HttpError, HttpRequest, HttpResponse};
use shiksha_companion_transport::keys::{load_or_create, MemoryStore, P256Key, Sha256Hasher};

const BASE: &str = "https://api.example.test/api/sessions/collaboration/v1";

/// Records every request and replays queued responses.
#[derive(Clone, Default)]
struct FakeHttp {
    sent: Arc<Mutex<Vec<HttpRequest>>>,
    replies: Arc<Mutex<Vec<Result<HttpResponse, HttpError>>>>,
}

impl FakeHttp {
    fn push(&self, r: Result<HttpResponse, HttpError>) {
        self.replies.lock().unwrap().push(r);
    }
    fn sent(&self) -> Vec<HttpRequest> {
        self.sent.lock().unwrap().clone()
    }
}

impl Http for FakeHttp {
    fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError> {
        self.sent.lock().unwrap().push(req.clone());
        let mut q = self.replies.lock().unwrap();
        assert!(!q.is_empty(), "unscripted request to {}", req.url);
        q.remove(0)
    }
}

fn ok(body: &str) -> Result<HttpResponse, HttpError> {
    Ok(HttpResponse { status: 200, body: body.as_bytes().to_vec(), date_unix: None, retry_after_s: None })
}

fn status(code: u16, body: &str) -> Result<HttpResponse, HttpError> {
    Ok(HttpResponse { status: code, body: body.as_bytes().to_vec(), date_unix: None, retry_after_s: None })
}

fn new_api(http: FakeHttp) -> DeviceApi<FakeHttp> {
    // A fixed key so runs are reproducible. 0x11.. is a valid P-256 scalar.
    let key = P256Key::from_secret_bytes(&[0x11u8; 32]).expect("valid scalar");
    DeviceApi::new(
        ApiConfig { canonical_base: BASE.into() },
        http,
        Proofs {
            signer: Box::new(key),
            hasher: Box::new(Sha256Hasher),
            entropy: Box::new(CountingEntropy::default()),
            offset: ClockOffset::default(),
        },
        Box::new(|| 1_700_000_000),
    )
}

/// Decode a compact JWT's header and payload.
fn decode(proof: &str) -> (Value, Value) {
    let parts: Vec<&str> = proof.split('.').collect();
    assert_eq!(parts.len(), 3, "a proof is three segments");
    let h = URL_SAFE_NO_PAD.decode(parts[0]).unwrap();
    let p = URL_SAFE_NO_PAD.decode(parts[1]).unwrap();
    (serde_json::from_slice(&h).unwrap(), serde_json::from_slice(&p).unwrap())
}

fn dpop_claims(req: &HttpRequest) -> Value {
    decode(req.header("DPoP").expect("a DPoP header")).1
}

// ── Request shape ─────────────────────────────────────────────────────────

#[test]
fn pairing_creation_sends_a_proof_without_ath_and_no_authorization() {
    let http = FakeHttp::default();
    http.push(Ok(HttpResponse {
        status: 201,
        body: br#"{"pairing_id":"p1","device_secret":"s3cret","user_code":"ABCD-EFGH","expires_at":"x","poll_interval_s":5}"#.to_vec(),
        date_unix: None,
        retry_after_s: None,
    }));
    let mut a = new_api(http.clone());
    let started = a.create_pairing("Laptop", "windows", "x64", "0.1.0");
    // 201 is not 200; `call` accepts any 2xx.
    let started = started.expect("201 is a success");
    assert_eq!(started.pairing_id, "p1");

    let req = &http.sent()[0];
    assert_eq!(req.method, "POST");
    assert_eq!(req.url, format!("{BASE}/pairings/"));
    assert!(req.header("Authorization").is_none());
    let claims = dpop_claims(req);
    assert!(claims.get("ath").is_none(), "the server rejects an unexpected ath");
    assert_eq!(claims["htm"], "POST");
    assert_eq!(claims["htu"], format!("{BASE}/pairings/"));
    assert_eq!(claims["iat"], 1_700_000_000);
    assert!(claims["jti"].as_str().unwrap().len() >= 16);
}

#[test]
fn the_registered_key_and_the_proof_key_are_the_same_key() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"pairing_id":"p","device_secret":"s","user_code":"c","expires_at":"x","poll_interval_s":5}"#));
    let mut a = new_api(http.clone());
    a.create_pairing("L", "windows", "x64", "0.1.0").unwrap();
    let req = &http.sent()[0];

    let body: Value = serde_json::from_str(req.body.as_ref().unwrap()).unwrap();
    let (header, _) = decode(req.header("DPoP").unwrap());
    assert_eq!(body["public_jwk"], header["jwk"], "else the server refuses with device_proof_invalid");
    assert_eq!(header["alg"], "ES256");
    assert_eq!(header["typ"], "dpop+jwt");
    assert!(header["jwk"].get("d").is_none(), "never a private key in the header");
}

#[test]
fn the_core_thumbprint_equals_an_independent_rfc7638_computation_of_the_header_jwk() {
    // The server identifies the device by this value, so it must equal
    // base64url(SHA-256({"crv":..,"kty":..,"x":..,"y":..})) computed from the key
    // the proof actually carries - by code that shares nothing with ours.
    let http = FakeHttp::default();
    http.push(ok(r#"{"pairing_id":"p","device_secret":"s","user_code":"c","expires_at":"x","poll_interval_s":5}"#));
    let mut a = new_api(http.clone());
    a.create_pairing("L", "windows", "x64", "0.1.0").unwrap();
    let (header, _) = decode(http.sent()[0].header("DPoP").unwrap());
    let jwk = &header["jwk"];
    let canonical = format!(
        r#"{{"crv":"{}","kty":"{}","x":"{}","y":"{}"}}"#,
        jwk["crv"].as_str().unwrap(),
        jwk["kty"].as_str().unwrap(),
        jwk["x"].as_str().unwrap(),
        jwk["y"].as_str().unwrap()
    );
    let independent = {
        use sha2::{Digest, Sha256};
        URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
    };

    let key = P256Key::from_secret_bytes(&[0x11u8; 32]).unwrap();
    let (x, y) = shiksha_companion_core::dpop::ProofSigner::public_xy(&key);
    assert_eq!(thumbprint(&Sha256Hasher, &x, &y), independent);
    assert_eq!(independent.len(), 43);
}

#[test]
fn a_resource_request_carries_the_access_token_and_an_ath_that_binds_it() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"server_time":"t","minimum_version":"","disabled":false}"#));
    let mut a = new_api(http.clone());
    a.heartbeat("the.access.token", "0.1.0", 1, 1, false).unwrap();

    let req = &http.sent()[0];
    assert_eq!(req.header("Authorization"), Some("DPoP the.access.token"));
    let claims = dpop_claims(req);
    let want = {
        use sha2::{Digest, Sha256};
        URL_SAFE_NO_PAD.encode(Sha256::digest(b"the.access.token"))
    };
    assert_eq!(claims["ath"], want);
    assert_eq!(claims["htu"], format!("{BASE}/device-heartbeat/"));
}

#[test]
fn poll_sends_the_secret_in_authorization_only_never_in_the_url_or_body() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"state":"waiting"}"#));
    let mut a = new_api(http.clone());
    a.poll_pairing("p1", "s3cret").unwrap();
    let req = &http.sent()[0];
    assert_eq!(req.header("Authorization"), Some("Pairing s3cret"));
    assert!(!req.url.contains("s3cret"));
    assert!(!req.body.as_ref().unwrap().contains("s3cret"));
    assert!(req.header("DPoP").is_none(), "the server does not read a proof on poll");
}

#[test]
fn finalize_needs_both_the_secret_and_a_proof_and_asserts_local_confirmation() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"device_id":"d1","access_token":"a","refresh_token":"r","expires_in":300}"#));
    let mut a = new_api(http.clone());
    let p = a.finalize_pairing("p1", "s3cret").unwrap();
    assert_eq!(p.device_id, "d1");
    let req = &http.sent()[0];
    assert_eq!(req.header("Authorization"), Some("Pairing s3cret"));
    assert!(req.header("DPoP").is_some());
    assert!(dpop_claims(req).get("ath").is_none());
    assert_eq!(req.url, format!("{BASE}/pairings/p1/finalize/"));
    let body: Value = serde_json::from_str(req.body.as_ref().unwrap()).unwrap();
    assert_eq!(body["local_confirm"], true);
}

#[test]
fn refresh_sends_device_id_which_the_contract_omits_and_no_ath() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"access_token":"a2","refresh_token":"r2","expires_in":300}"#));
    let mut a = new_api(http.clone());
    let attempt = RefreshAttempt { device_id: "d1".into(), refresh_token: "r1".into(), started: Mono(0) };
    let out = a.refresh(&attempt);
    assert_eq!(
        out,
        RefreshOutcome::Rotated { access_token: "a2".into(), refresh_token: "r2".into(), expires_in_s: 300 }
    );
    let req = &http.sent()[0];
    let body: Value = serde_json::from_str(req.body.as_ref().unwrap()).unwrap();
    assert_eq!(body["device_id"], "d1");
    assert_eq!(body["refresh_token"], "r1");
    assert!(req.header("Authorization").is_none());
    assert!(dpop_claims(req).get("ath").is_none());
}

#[test]
fn every_proof_has_a_fresh_jti_and_the_lease_route_is_never_replayed() {
    let http = FakeHttp::default();
    for _ in 0..2 {
        http.push(ok(r#"{"lease_jwt":"l"}"#));
    }
    let mut a = new_api(http.clone());
    a.lease("t", "g", "e", 4, "nonce-1234567890abcd").unwrap();
    a.lease("t", "g", "e", 4, "nonce-1234567890abce").unwrap();
    let s = http.sent();
    assert_ne!(dpop_claims(&s[0])["jti"], dpop_claims(&s[1])["jti"]);
    assert!(s[0].header("Idempotency-Key").is_none(), "a replayed lease outlives its deadline");
}

#[test]
fn activate_carries_the_lease_nonce_and_the_local_confirmation() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"grant":{},"activation_jwt":"A","lease_jwt":"L"}"#));
    let mut a = new_api(http.clone());
    let act = a.activate("t", "g1", "cn", 3, 7, "LN").unwrap();
    assert_eq!((act.activation_jwt.as_str(), act.lease_jwt.as_str()), ("A", "L"));
    let body: Value = serde_json::from_str(http.sent()[0].body.as_ref().unwrap()).unwrap();
    assert_eq!(body["lease_nonce"], "LN");
    assert_eq!(body["local_confirm"], true);
    assert_eq!(body["capture_generation"], 7);
    assert_eq!(body["expected_version"], 3);
}

// ── Failure handling ──────────────────────────────────────────────────────

#[test]
fn a_server_error_envelope_is_surfaced_with_its_code() {
    let http = FakeHttp::default();
    http.push(status(409, r#"{"code":"version_conflict","detail":"stale","request_id":"r"}"#));
    let mut a = new_api(http);
    let e = a.lease("t", "g", "e", 1, "n-1234567890abcdef").unwrap_err();
    assert_eq!(
        e,
        ApiError::Server { status: 409, code: "version_conflict".into(), detail: "stale".into(), retry_after_s: None }
    );
}

#[test]
fn a_2xx_with_a_garbled_body_is_a_bad_body_not_a_success() {
    let http = FakeHttp::default();
    http.push(ok("not json"));
    let mut a = new_api(http);
    assert_eq!(a.lease("t", "g", "e", 1, "n-1234567890abcdef").unwrap_err(), ApiError::BadBody("not_an_object"));
}

#[test]
fn a_missing_field_in_a_2xx_names_the_field() {
    let http = FakeHttp::default();
    http.push(ok(r#"{"state":"waiting"}"#));
    let mut a = new_api(http.clone());
    // poll needs only `state`; share needs several.
    a.poll_pairing("p", "s").unwrap();
    http.push(ok(r#"{"token":"t"}"#));
    assert_eq!(a.share("t", "hb", Value::Null).unwrap_err(), ApiError::BadBody("livekit_url"));
}

#[test]
fn the_server_clock_is_learned_from_every_reply_and_corrects_the_next_proof() {
    let http = FakeHttp::default();
    // Server says it is 1_700_000_100; our wall clock says 1_700_000_000.
    http.push(Ok(HttpResponse {
        status: 401,
        body: br#"{"code":"device_proof_invalid"}"#.to_vec(),
        date_unix: Some(1_700_000_100),
        retry_after_s: None,
    }));
    http.push(ok(r#"{"access_token":"a","refresh_token":"r","expires_in":300}"#));
    let mut a = new_api(http.clone());
    let attempt = RefreshAttempt { device_id: "d".into(), refresh_token: "r0".into(), started: Mono(0) };
    assert_eq!(a.refresh(&attempt), RefreshOutcome::ProofRejected);
    assert_eq!(a.clock_offset().seconds(), 100);
    a.refresh(&attempt);
    let s = http.sent();
    assert_eq!(dpop_claims(&s[0])["iat"], 1_700_000_000);
    assert_eq!(dpop_claims(&s[1])["iat"], 1_700_000_100, "second proof uses the server's time");
}

// ── Refresh classification: the part where a wrong answer revokes a device ──

fn resp(status: u16, body: &str, retry: Option<u64>) -> Result<HttpResponse, ApiError> {
    Ok(HttpResponse { status, body: body.as_bytes().to_vec(), date_unix: None, retry_after_s: retry })
}

#[test]
fn classification_table() {
    use RefreshOutcome::*;
    let good = r#"{"access_token":"a","refresh_token":"r","expires_in":300}"#;
    assert!(matches!(classify_refresh(resp(200, good, None)), Rotated { .. }));

    // The server rotated but we cannot read the new token: NOT an error we can
    // retry, because the old token is now spent.
    assert_eq!(classify_refresh(resp(200, "garbage", None)), Ambiguous);
    assert_eq!(classify_refresh(resp(200, r#"{"access_token":"a"}"#, None)), Ambiguous);
    assert_eq!(
        classify_refresh(resp(200, r#"{"access_token":"","refresh_token":"","expires_in":300}"#, None)),
        Ambiguous
    );

    assert_eq!(classify_refresh(resp(401, r#"{"code":"device_proof_invalid"}"#, None)), ProofRejected);
    for code in DEAD_CODES {
        let body = format!(r#"{{"code":"{code}"}}"#);
        assert_eq!(classify_refresh(resp(401, &body, None)), Rejected { code: code.to_string() });
    }
    // An unrecognised or unreadable 401 must not erase a credential.
    assert_eq!(classify_refresh(resp(401, r#"{"code":"something_new"}"#, None)), Ambiguous);
    assert_eq!(classify_refresh(resp(401, "<html>", None)), Ambiguous);

    assert_eq!(classify_refresh(resp(429, "{}", Some(12))), RateLimited { retry_after_s: 12 });
    assert_eq!(classify_refresh(resp(429, "{}", None)), RateLimited { retry_after_s: 30 });

    // 5xx and other 4xx: the server may have committed.
    for s in [400u16, 403, 404, 409, 410, 500, 502, 503] {
        assert_eq!(classify_refresh(resp(s, "{}", None)), Ambiguous, "{s}");
    }

    assert_eq!(
        classify_refresh(Err(ApiError::Http(HttpError::NotSent("dns".into())))),
        NotSent
    );
    assert_eq!(
        classify_refresh(Err(ApiError::Http(HttpError::Ambiguous("timeout".into())))),
        Ambiguous
    );
}

// ── End to end with the credential state machine ──────────────────────────

#[test]
fn a_refresh_whose_reply_is_lost_waits_then_retries_once_and_never_sends_two_at_once() {
    let http = FakeHttp::default();
    http.push(Err(HttpError::Ambiguous("timed out".into())));
    http.push(ok(r#"{"access_token":"a2","refresh_token":"r2","expires_in":300}"#));
    let mut api = new_api(http.clone());
    let mut creds = Credentials::from_pairing("d".into(), "a1".into(), "r1".into(), 300, Mono(0));

    let a1 = creds.begin_refresh(Mono(250_000)).unwrap();
    assert!(creds.begin_refresh(Mono(250_001)).is_none(), "one in flight at a time");
    let out = api.refresh(&a1);
    creds.on_refresh(&a1, out, Mono(265_000));
    assert!(creds.begin_refresh(Mono(265_001)).is_none(), "waiting out the possible first attempt");

    let a2 = creds.begin_refresh(Mono(325_000)).expect("the single retry");
    assert_eq!(a2.refresh_token, "r1", "same token: it was never confirmed spent");
    let out = api.refresh(&a2);
    let fx = creds.on_refresh(&a2, out, Mono(325_100));
    assert_eq!(http.sent().len(), 2);
    assert!(!fx.is_empty(), "the rotated token is handed over to persist");
}

// ── Keys ──────────────────────────────────────────────────────────────────

#[test]
fn the_key_is_created_once_and_then_reloaded_not_replaced() {
    let store = MemoryStore::default();
    let first = load_or_create(&store).unwrap();
    let second = load_or_create(&store).unwrap();
    let (x1, y1) = shiksha_companion_core::dpop::ProofSigner::public_xy(&first);
    let (x2, y2) = shiksha_companion_core::dpop::ProofSigner::public_xy(&second);
    assert_eq!((x1, y1), (x2, y2), "replacing the key would orphan the pairing");
}

#[test]
fn a_corrupt_stored_key_is_an_error_not_a_silent_replacement() {
    use shiksha_companion_transport::keys::{SecretStore, KEY_NAME};
    let store = MemoryStore::default();
    store.put(KEY_NAME, b"short").unwrap();
    assert!(load_or_create(&store).is_err());
}

#[test]
fn debug_output_never_shows_secrets() {
    let key = P256Key::from_secret_bytes(&[0x11u8; 32]).unwrap();
    assert_eq!(format!("{key:?}"), "P256Key(<redacted>)");
    let p = PairedDevice {
        device_id: "d".into(),
        access_token: "ACCESS".into(),
        refresh_token: "REFRESH".into(),
        expires_in_s: 300,
    };
    let s = format!("{p:?}");
    assert!(!s.contains("ACCESS") && !s.contains("REFRESH"));
}
