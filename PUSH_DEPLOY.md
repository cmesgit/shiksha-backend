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

## What is NOT done

* **Nothing is deployed.** This branch has never run on dev or prod.
* **No push has ever been delivered to a real device.** The client registers
  tokens and the server stores them; an end-to-end send is unproven.
* **iOS**: `type` is hardcoded `"android"` in the client because iOS has never
  been built. APNs would need its own certificate in the Firebase project.
