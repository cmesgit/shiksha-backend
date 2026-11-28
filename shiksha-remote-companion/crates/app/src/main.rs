//! Composition root.
//!
//! ## What works today
//! * `--pair`: pair this machine with an account (the owner confirms on the
//!   console — a **development stand-in** for the native consent UI, RC-014).
//! * default: restore the saved credentials and run the device worker (token
//!   refresh and device heartbeat) plus the watchdog tick. No room is joined.
//!
//! ## What does not exist yet
//! Sharing a display, joining a room and being controlled need the Windows
//! capture/input adapter (RC-013) and the native consent UI (RC-014). The only
//! input adapter wired in is [`RefusingAdapter`], which fails every call, so a
//! build that forgets to wire a real one injects nothing.
//!
//! Usage: `shiksha-companion --api-base <canonical url> [--pair] [--dev-http]`
//! where `<canonical url>` is the server's `COLLABORATION_CANONICAL_URL`
//! including the route prefix, e.g.
//! `https://api.shikshacom.com/api/sessions/collaboration/v1`.
//!
//! NOT YET COMPILED (see HANDOFF).

use std::io::{BufRead, Write};
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use shiksha_companion_core::adapter::{AdapterError, InputAdapter, Release, ValidatedEvent};
use shiksha_companion_core::clock::{Mono, MonotonicClock, SystemClock};
use shiksha_companion_core::companion::{Companion, DeviceIdentity, InputOrigin, StopLatch};
use shiksha_companion_core::credentials::Credentials;
use shiksha_companion_core::dpop::{thumbprint, ClockOffset, ProofSigner};
use shiksha_companion_core::drivers::HeartbeatSchedule;
use shiksha_companion_core::secrets::{
    SecretStore, DEVICE_ID_NAME, REFRESH_INFLIGHT_NAME, REFRESH_NAME,
};
use shiksha_companion_platform_windows::dpapi::DpapiStore;
#[cfg(windows)]
use shiksha_companion_platform_windows::grant::GrantKit;
use shiksha_companion_platform_windows::provenance::PhysicalState;
use shiksha_companion_transport::device_api::{ApiConfig, DeviceApi, Proofs};
use shiksha_companion_transport::entropy::OsEntropy;
use shiksha_companion_transport::http::ReqwestHttp;
use shiksha_companion_transport::keys::{load_or_create, P256Key, Sha256Hasher};
use shiksha_companion_transport::runtime::{lock_driver, pair, DeviceWorker, PairingUi};
use shiksha_companion_transport::session::Driver;

const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
const PROTOCOL: (u64, u64) = (1, 1);
const WATCHDOG_PERIOD: Duration = Duration::from_millis(50);
const WORKER_PERIOD: Duration = Duration::from_millis(500);
/// Device worker HTTP timeout. Long is fine: it never carries a lease.
const DEVICE_HTTP_TIMEOUT: Duration = Duration::from_secs(20);

/// Stand-in until the Windows adapter (RC-013) exists. Failing closed is the
/// point: a build that forgets to wire a real adapter injects nothing.
struct RefusingAdapter;

impl InputAdapter for RefusingAdapter {
    fn supports_key(&self, _code: &str) -> bool {
        false
    }
    fn inject(&mut self, _event: &ValidatedEvent) -> Result<(), AdapterError> {
        Err(AdapterError("no_os_adapter_until_rc013"))
    }
    fn release(&mut self, _what: &Release) -> Result<(), AdapterError> {
        Ok(())
    }
}

/// Console consent, **for development only**. A keyboard answer on the console
/// is reported as physical, which is a promise the console cannot keep: a script
/// can type it. RC-014 replaces this with a native dialog whose provenance the
/// Windows adapter can actually check.
struct ConsoleUi;

impl PairingUi for ConsoleUi {
    fn show_code(&mut self, user_code: &str) {
        println!("\nOn a signed-in browser, open Shiksha settings > Devices and enter this code:\n\n    {user_code}\n");
        println!("Waiting for you to enter it...");
    }

    fn ask_owner(&mut self, account: &str, phrase: &str) -> (InputOrigin, bool) {
        println!("\nThe browser says this is account: {account}");
        println!("Verification phrase: {phrase}");
        print!("Does the browser show the same phrase? Pair this computer? [y/N] ");
        let _ = std::io::stdout().flush();
        let mut line = String::new();
        let _ = std::io::stdin().lock().read_line(&mut line);
        (InputOrigin::Physical, matches!(line.trim(), "y" | "Y" | "yes"))
    }
}

struct Args {
    api_base: String,
    pair: bool,
    dev_http: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args { api_base: String::new(), pair: false, dev_http: false };
    let mut it = std::env::args().skip(1);
    while let Some(x) = it.next() {
        match x.as_str() {
            "--api-base" => a.api_base = it.next().ok_or("--api-base needs a value")?,
            "--pair" => a.pair = true,
            "--dev-http" => a.dev_http = true,
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    if a.api_base.is_empty() {
        return Err("--api-base is required".into());
    }
    Ok(a)
}

fn unix_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn make_api(
    base: &str,
    secret: &[u8],
    https_only: bool,
    timeout: Duration,
) -> Result<DeviceApi<ReqwestHttp>, String> {
    let key = P256Key::from_secret_bytes(secret).map_err(|e| format!("device key: {e:?}"))?;
    let http = ReqwestHttp::new(https_only, timeout)?;
    Ok(DeviceApi::new(
        ApiConfig { canonical_base: base.to_string() },
        http,
        Proofs {
            signer: Box::new(key),
            hasher: Box::new(Sha256Hasher),
            entropy: Box::new(OsEntropy),
            offset: ClockOffset::default(),
        },
        Box::new(unix_now),
    ))
}

fn main() {
    if let Err(e) = run() {
        eprintln!("shiksha-companion: {e}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    let store = DpapiStore::open_default().map_err(|e| format!("secret store: {}", e.0))?;
    let key = load_or_create(&store).map_err(|e| format!("device key: {e:?}"))?;
    let secret = key.secret_bytes();
    let clock = SystemClock::new(); // one clock, copied: Mono values are shared
    let https_only = !args.dev_http;

    if args.pair {
        let mut api = make_api(&args.api_base, &secret, https_only, DEVICE_HTTP_TIMEOUT)?;
        let creds = pair(
            &mut api,
            &clock,
            &mut ConsoleUi,
            &store,
            &hostname(),
            "windows",
            std::env::consts::ARCH,
            APP_VERSION,
        )
        .map_err(|e| format!("pairing did not complete: {e:?}"))?;
        match creds.needs_pairing() {
            None => println!("\nPaired."),
            Some(why) => println!("\nPaired, but this will not last: {why:?}. Pair again."),
        }
        return Ok(());
    }

    // ── Normal run ──
    let device_id = read_string(&store, DEVICE_ID_NAME)?;
    let refresh = read_string(&store, REFRESH_NAME)?;
    let (device_id, refresh) = match (device_id, refresh) {
        (Some(d), Some(r)) => (d, r),
        _ => return Err("this computer is not paired; run with --pair".into()),
    };
    // A marker means the previous run died mid-refresh: the outcome is unknown.
    let in_flight = store.get(REFRESH_INFLIGHT_NAME).map_err(|e| e.0.to_string())?.is_some();
    let creds = Arc::new(Mutex::new(Credentials::restored(device_id, refresh, in_flight, clock.now())));

    // Print the thumbprint so an operator can match it to the account's device list.
    let (x, y) = key.public_xy();
    println!("device key thumbprint: {}", thumbprint(&Sha256Hasher, &x, &y));

    let latch = StopLatch::new();
    // Held-modifier state, shared by the input monitor and (later) the adapter.
    let physical = Arc::new(PhysicalState::new());
    // The physical Stop chord. Without it there is no emergency Stop, so the
    // companion is told so and nothing may ever activate (core enforces it).
    // The chord is Win32-only (RegisterHotKey); off Windows there is no
    // implementation to call, so the companion always runs with it absent.
    #[cfg(windows)]
    let kit = match GrantKit::new(latch.clone(), physical.clone()) {
        Ok(k) => {
            println!("Emergency Stop: {}", k.stop_shortcut());
            Some(k)
        }
        Err(e) => {
            eprintln!("Emergency Stop chord unavailable ({e:?}): remote control will not be offered.");
            None
        }
    };
    #[cfg(not(windows))]
    let kit: Option<()> = {
        let _ = &physical; // only the Windows chord above consumes it
        eprintln!("Emergency Stop chord unavailable (not running on Windows): remote control will not be offered.");
        None
    };
    let mut companion = Companion::new(
        RefusingAdapter,
        clock,
        OsEntropy,
        latch,
        DeviceIdentity { device_id: String::new(), key_thumbprint: String::new() },
    );
    companion.set_emergency_stop_ready(kit.is_some());
    let driver = Arc::new(Mutex::new(Driver::new(companion)));

    // Watchdog: independent of every network thread. It also notices the Stop
    // chord when no grant guard is polling (the latch is already closed; this
    // only runs the cleanup, and is idempotent with the guard's own poll).
    {
        let driver = driver.clone();
        thread::Builder::new()
            .name("watchdog".into())
            .spawn(move || loop {
                thread::sleep(WATCHDOG_PERIOD);
                let mut d = lock_driver(&driver);
                let _ = d.tick();
                // Keeps `kit` referenced when the Windows-only check below is
                // compiled out, so the closure still needs to capture it.
                let _ = &kit;
                #[cfg(windows)]
                if kit.as_ref().map_or(false, |k| k.take_chord_idle()) {
                    let _ = d.companion_mut().sync_after_latch_stop();
                }
            })
            .map_err(|e| e.to_string())?;
    }

    let mut worker = DeviceWorker {
        api: make_api(&args.api_base, &secret, https_only, DEVICE_HTTP_TIMEOUT)?,
        creds: creds.clone(),
        store: Arc::new(store),
        clock: Box::new(clock),
        heartbeat: HeartbeatSchedule::new(Mono(0)),
        app_version: APP_VERSION.to_string(),
        protocol: PROTOCOL,
        sharing: Arc::new(AtomicBool::new(false)),
        disabled: Arc::new(AtomicBool::new(false)),
    };

    println!("shiksha-companion running: refresh + heartbeat only. No room joined, no OS input adapter (RC-013/014).");
    loop {
        worker.step();
        if let Some(why) = creds.lock().ok().and_then(|c| c.needs_pairing().cloned()) {
            return Err(format!("credentials are no longer usable ({why:?}); run with --pair"));
        }
        thread::sleep(WORKER_PERIOD);
    }
}

fn read_string(store: &dyn SecretStore, name: &str) -> Result<Option<String>, String> {
    match store.get(name).map_err(|e| format!("{name}: {}", e.0))? {
        Some(b) => String::from_utf8(b).map(Some).map_err(|_| format!("{name}: not text")),
        None => Ok(None),
    }
}

fn hostname() -> String {
    // Shown to the owner in their device list. Never sent anywhere else.
    std::env::var("COMPUTERNAME").unwrap_or_else(|_| "Windows computer".to_string())
}
