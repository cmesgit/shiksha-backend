# Deploying push notifications

Written 2026-11-21 alongside `feat/fcm-push`. Read before deploying this
branch — two of the steps below are easy to get wrong in ways that are silent.

## What this branch adds

| | |
| --- | --- |
| `fcm_django` in `INSTALLED_APPS` | stores device tokens (ships its own migrations) |
| `POST /api/push/devices/` | device registration, `FCMDeviceAuthorizedViewSet` |
| `FIREBASE_APP` in settings | best-effort init from a service-account JSON |
| `notifications/push.py` | already existed as a no-op; now actually sends |

Nothing else changed. `notifications.services.notify()` has always called
`push_to_users`, so **every verb that already notifies starts delivering push
the moment a credential lands** — no further code change.

## Order of operations

🔴 **The client can ship BEFORE the credential.** `fcm_django` is installed
unconditionally and `FIREBASE_APP` is allowed to be None, so tokens register
and accumulate while sends no-op. That ordering is deliberate: deploy the
backend, ship the app, let tokens arrive, then add the key and push starts
working for everyone already registered. The reverse — key first, client
later — gains nothing.

1. `pip install -r requirements.txt` (see the dependency warning below)
2. `python manage.py migrate` — creates `fcm_django_fcmdevice`
3. Restart. **Push is live but silent**: tokens store, sends no-op.
4. Copy the service-account JSON to the box, out of band.
5. Set `FIREBASE_SERVICE_ACCOUNT_JSON=/path/to/firebase-adminsdk.json`
6. Restart. Sends begin.

## 🔴 The dependency bump is the risky part

`firebase-admin` drags four existing pins forward, and they are bumped in
`requirements.txt` deliberately so a deploy installs the set that was actually
exercised rather than whatever pip resolves on the day:

    cryptography    46.0.6 -> 50.0.2
    google-api-core 2.30.0 -> 2.44.0
    PyJWT           2.10.1 -> 2.15.1
    requests        2.32.5 -> 2.34.2

**`cryptography` is the one to watch.** `signxml` verifies Aadhaar offline
e-KYC signatures in the scholarship app and sits directly on it. Locally:
`signxml`, `cryptography` and `pyOpenSSL` all import, `manage.py check` passes,
and `scholarship` + `accounts` tests pass on 50.0.2. `pip check` still reports
`pyopenssl` wanting `cryptography<49` — a resolver complaint, not an observed
runtime break. **Smoke-test a real Aadhaar verification after deploying
anyway**; a test suite is not a signed XAdES document.

## 🔴 The credential

* It is a **real secret** — full access to the Firebase project.
* `FIREBASE_SERVICE_ACCOUNT_JSON` is a **path**, never the JSON itself, so it
  cannot end up in an env dump, a process list or a log line.
* It is currently on **one laptop only**, at
  `shiksha-backend/secrets/firebase-adminsdk.json` (mode 600, gitignored). It
  is on neither server.
* Firebase will not re-issue it. Lost means generate a new key and revoke the
  old one in the console.

## Why initialisation is wrapped in try/except

Settings run on every management command, every worker and every test. An
unguarded `initialize_app()` means one missing or malformed file takes the
**whole site** down, not just push — and it presents as Django failing to
boot, which reads as a broken deploy rather than a credential problem. So it
warns, leaves `FIREBASE_APP = None`, and `push_to_users` short-circuits.

That short-circuit is also why there is no log spam: without it, every
recipient of every one of the ~45 push-eligible verbs would log a full
`ValueError: The default Firebase app does not exist` traceback.

## Deployment status

### ✅ DEV — deployed 2026-11-21 (`ad62d5c`)

Merged to `dev` and deployed by hand; `shiksha-backend` has no deploy Action.

```
git pull --ff-only origin dev
./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py migrate        # fcm_django 0001..0011
sudo systemctl restart uvicorn celery celery-beat
```

Verified ON THE BOX:

* `uvicorn`, `celery`, `celery-beat` all report `SubState=running` — `is-active`
  is not a reliable signal here, use `SubState`.
* `POST /api/push/devices/` → **401** unauthenticated. Endpoint live and
  protected.
* `skill/categories/` and `courses/public/catalog/` still **200** — no
  regression from the dependency bump.
* **Zero** errors or tracebacks in `journalctl -u uvicorn` after restart.
* `signxml`, `cryptography 50.0.2`, `pyOpenSSL 26.4.0` all import and
  `XMLVerifier` resolves.
* 🔴 **`pip check` on dev: "No broken requirements found."** The
  `pyopenssl wants cryptography<49` complaint seen locally is GONE, because
  installing from requirements.txt also pulled `pyopenssl` to 26.4.0, which
  supports cryptography 50. The local venv is the stale one, not dev.

### ✅ The credential is installed on dev — sending is LIVE

Added 2026-11-21, after the code deploy.

* Key at `/home/appuser/backend/secrets/firebase-adminsdk.json` — **mode 600,
  dir 700, owned by appuser, and OUTSIDE the git checkout** so it cannot be
  committed by accident. Verified intact on the box: project `shikshacom`,
  `firebase-adminsdk-fbsvc@…`, private key present.
* `.env` gained `FIREBASE_SERVICE_ACCOUNT_JSON=<path>` (a path, never the
  JSON, so it cannot leak through an env dump or a process list). The previous
  `.env` was backed up to `.env.bak-prefcm-<timestamp>` first.
* `settings.FIREBASE_APP` reports **INITIALISED**.

🔴 **"Initialised" is not "can reach Google", so that was proven separately.**
A `messaging.send(..., dry_run=True)` with a deliberately bogus token returned
**`InvalidArgumentError: The registration token is not a valid FCM
registration token`** — FCM evaluated the token, which means the credential
*authenticated*. A bad credential fails earlier and differently
(`UnauthenticatedError` / permission denied). Nothing was delivered: `dry_run`
validates only.

⚠ Noted, not fixed: firebase-admin 7.8 warns `Message.token is deprecated, use
Message.fid`. Harmless today — real delivery works — but it will need
attention on a future SDK bump.

⚠ **The authenticated round trip was proven on the LOCAL stack, not on dev** —
registering a device on dev needs a dev account credential. What dev proves is
that the route exists, rejects anonymous callers, and did not break anything.

### ⏭ Aadhaar e-KYC re-verification — DEFERRED BY THE OWNER, work for later

🔴 **This is a known, accepted, OPEN risk — not an oversight, and not done.**
`firebase-admin` moved `cryptography` 46 → 50, and `signxml` verifies Aadhaar
offline e-KYC signatures on top of it (scholarship app). What has been checked
is only that `signxml`, `cryptography` and `pyOpenSSL` import, that
`XMLVerifier` resolves, that `pip check` is clean, and that 275
scholarship+accounts tests pass.

**None of that exercises a real signed XAdES document.** A signature library
can import perfectly and still reject — or wrongly accept — a real UIDAI
signature after a major version bump. The failure mode is quiet: scholarship
applicants fail verification, or worse, pass when they should not.

**What to do when it is picked up:** run one real Aadhaar offline e-KYC XML
through the scholarship verification path on dev and confirm it validates
exactly as it did before. Then do the same on prod after deployment.

The owner has explicitly deferred this to later work. Do not treat the deploy
as proof it is fine.

### ✅ PROD — deployed 2026-11-22 (`030ba1db`), CODE ONLY

A fresh 6.1 MB DB backup was taken first and its **size checked** — a prod
`pg_dump` has previously failed silently with exit 0 and a 20-byte file.

```
sudo systemctl start shiksha-db-backup.service    # then CHECK THE SIZE
git pull --ff-only origin main
./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py check                 # BEFORE migrating
./venv/bin/python manage.py migrate               # fcm_django 0001..0011 only
sudo systemctl restart uvicorn celery celery-beat
```

🔴 **Sending is OFF on prod, deliberately.** `FIREBASE_APP` is `None` — the
credential was NOT installed. Real users' devices register tokens and bank
them; nothing can notify anyone by accident. Adding the key and restarting
turns sending on, and every token collected until then starts working at that
moment.

⚠ **This release also shipped gasey's TeacherContextView fix**, which had been
merged to main and undeployed for ~24h. Prod was 2 commits behind main, so
there was no way to release push without it. Confirmed with the owner first
rather than shipped quietly.

Verified from OUTSIDE, over public HTTPS — not just localhost:

* `api.shikshacom.com/api/skill/categories/` → **200**
* `api.shikshacom.com/api/courses/public/catalog/` → **200**
* `api.shikshacom.com/api/push/devices/` → **401** unauthenticated
* `shikshacom.com` → **200**
* All three units `SubState=running`, **zero** tracebacks after restart
* `cryptography 50.0.2`, `pyOpenSSL 26.4.0`, `XMLVerifier` resolves,
  `pip check` clean

### ⏭ Not deployed

## What is NOT done

* **Prod has not been touched.**
* **No push has ever been delivered to a real device.** The client registers
  tokens and the server stores them; an end-to-end send is unproven.
* **iOS**: `type` is hardcoded `"android"` in the client because iOS has never
  been built. APNs would need its own certificate in the Firebase project.
