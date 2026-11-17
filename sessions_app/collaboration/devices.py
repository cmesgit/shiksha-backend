"""Pairing a desktop, and the credentials it gets (RC-007a).

The handshake is three steps on two devices, and that is a security
property rather than a UX choice:

    POST pairings/              companion  proves a fresh P-256 key
    POST pairings/confirm/      browser    types the 10-character code
    POST pairings/{id}/poll/    companion  waits, holding the secret
    POST pairings/{id}/finalize/companion  confirms LOCALLY, gets credentials

🔴 **A stolen browser session cannot pair, and neither can a stolen code.**
The browser step names the account; the companion step proves possession of
the key *and* a local human action on the desktop. T02 is exactly the claim
that neither alone suffices, so nothing may ever collapse these into one
call — not "for convenience", not for a test fixture.

🔴 **Secrets are generated here and never stored.** The device secret and
the user code exist in exactly one response each. What persists is a keyed
hash: HMAC-SHA256 under a server key, so a database copy does not yield
either value, and the code stays *findable* without a plaintext column.

🔴 **Device credentials are subordinate.** The access token carries
`typ=shiksha-device+jwt`, a `cnf.jkt` binding it to the device key, and
scopes that name only companion operations. `authenticate_device` refuses a
token lacking any of those, so a device credential can never be presented to
an ordinary user API — and an ordinary user token can never be presented
here.

RC-007b adds the other half: what happens to a credential *after* it is
issued — rotation with reuse detection, liveness, and revocation.

🔴 **Refresh reuse is treated as theft, not as a retry.** Rotation is
atomic and the previous hash is kept for exactly one generation, so
presenting a spent refresh token means two parties hold the credential:
either the device replayed (harmless but indistinguishable) or someone
stole it (not harmless). The safe reading of an ambiguous signal here is
the hostile one, so the device is revoked and the owner must re-pair.
"""
import hashlib
import hmac
import logging
import secrets
import uuid

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import dpop
from .policy import PolicyError

logger = logging.getLogger(__name__)

# companion-architecture.md: "a random 10-character human code from a
# 32-symbol alphabet". Crockford-style: no I, L, O, U — the characters people
# mis-transcribe, and U so the alphabet cannot spell unfortunate words.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 10
CODE_COLLISION_RETRIES = 5

DEVICE_SECRET_BYTES = 32          # "a 256-bit device secret"
ACCESS_TOKEN_TTL_SECONDS = 300    # "exp ≤5 minutes"
REFRESH_TOKEN_BYTES = 32          # "random 256-bit opaque values"
REFRESH_TOKEN_TTL_DAYS = 30

ACCESS_TOKEN_TYP = "shiksha-device+jwt"
ACCESS_TOKEN_AUDIENCE = "shiksha-companion-api"
ACCESS_TOKEN_ALG = "HS256"
DEVICE_SCOPES = ("companion:host-binding", "companion:grant", "companion:transfer")

# Two unrelated words, so the phrase is easy to compare across two screens
# and hard to confuse with a code worth typing.
_PHRASE_LEFT = ("amber", "brisk", "clever", "dusty", "eager", "fleet",
                "gentle", "humble", "ivory", "jolly", "keen", "lucid")
_PHRASE_RIGHT = ("anchor", "basket", "cedar", "dolphin", "ember", "falcon",
                 "garden", "harbor", "island", "jungle", "kettle", "lantern")


class PairingError(PolicyError):
    """Same envelope as every other collaboration refusal."""


# ── keyed hashing ─────────────────────────────────────────────────────────


def _server_key() -> bytes:
    """The key under which pairing secrets are hashed.

    Distinct from `SECRET_KEY` by configuration and from SimpleJWT's signing
    key by purpose — `deployment-rollout.md` requires a "separate signing key
    for control/device tokens". It falls back to a *derived* value rather
    than to SECRET_KEY itself so that a deployment which never sets it still
    does not reuse the session-signing secret verbatim.
    """
    configured = getattr(settings, "COLLABORATION_DEVICE_KEY", "") or ""
    if configured:
        return configured.encode()
    return hashlib.sha256(
        b"shiksha-collaboration-device-v1:" + settings.SECRET_KEY.encode()
    ).digest()


def keyed_hash(value: str) -> bytes:
    """HMAC-SHA256. Deterministic, so a code stays findable; keyed, so a
    database copy does not yield the plaintext."""
    return hmac.new(_server_key(), value.encode(), hashlib.sha256).digest()


def keyed_hash_hex(value: str) -> str:
    return keyed_hash(value).hex()


def hashes_match(value: str, stored) -> bool:
    if stored is None:
        return False
    if isinstance(stored, memoryview):
        stored = stored.tobytes()
    return hmac.compare_digest(keyed_hash(value), bytes(stored))


# ── generation ────────────────────────────────────────────────────────────


def new_user_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def new_device_secret() -> str:
    return secrets.token_urlsafe(DEVICE_SECRET_BYTES)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def new_verification_phrase() -> str:
    return f"{secrets.choice(_PHRASE_LEFT)}-{secrets.choice(_PHRASE_RIGHT)}"


def format_code(code: str) -> str:
    """`ABCDE-FGHIJ` for display. The wire value is always unformatted."""
    return f"{code[:5]}-{code[5:]}" if len(code) == CODE_LENGTH else code


def normalise_code(raw) -> str:
    """Accept what a human types: spacing, hyphens and case are not secrets.

    🔴 Normalising is NOT the same as widening the alphabet — a character
    outside the 32 symbols still fails to match any stored hash, because the
    lookup is over the hash of the normalised string.
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper().replace("-", "").replace(" ", "")


# ── access tokens ─────────────────────────────────────────────────────────


def mint_access_token(device, thumbprint):
    """A 5-minute, key-bound, scope-limited device token.

    `cnf.jkt` is the proof-of-possession binding from RFC 9449: the token is
    only usable by whoever can sign a DPoP proof with the matching key, so
    exfiltrating the token alone buys nothing.
    """
    now = int(timezone.now().timestamp())
    claims = {
        "iss": getattr(settings, "COLLABORATION_CANONICAL_URL", "") or "shiksha",
        "aud": ACCESS_TOKEN_AUDIENCE,
        "sub": str(device.id),
        "owner": str(device.owner_id),
        "cnf": {"jkt": thumbprint},
        "scopes": list(DEVICE_SCOPES),
        "cred_ver": device.credential_version,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        claims, _server_key(), algorithm=ACCESS_TOKEN_ALG,
        headers={"typ": ACCESS_TOKEN_TYP},
    )


def decode_access_token(token):
    """Decode and structurally validate a device access token.

    🔴 The algorithm list is fixed and `typ` is checked explicitly. Without
    the `typ` check an ordinary SimpleJWT user token signed with a colliding
    key would decode here and authorize a device route — the exact
    "device credentials cannot call user APIs" inversion, in reverse.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise PairingError("device_token_invalid", "Invalid device token.",
                           status=401) from exc
    if header.get("typ") != ACCESS_TOKEN_TYP:
        raise PairingError("device_token_invalid", "Invalid device token.",
                           status=401)
    try:
        return jwt.decode(
            token, _server_key(), algorithms=[ACCESS_TOKEN_ALG],
            audience=ACCESS_TOKEN_AUDIENCE,
            options={"require": ["exp", "sub", "cnf", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise PairingError("device_token_invalid", "Invalid device token.",
                           status=401) from exc


def authenticate_device(request, *, path):
    """Resolve the calling device from its access token + DPoP proof.

    Both are required. The proof must carry `ath` bound to this very token
    and must be signed by the key the token was issued against, so neither
    half is useful without the other.
    """
    from sessions_app.models import PairedDevice

    header = request.headers.get("Authorization", "")
    if not header.startswith("DPoP "):
        # Deliberately not "Bearer": a device credential must never be
        # accepted by code that is looking for an ordinary bearer token.
        raise PairingError("device_auth_required",
                           "Device authentication required.", status=401)
    token = header[5:].strip()
    claims = decode_access_token(token)

    try:
        thumbprint, _ = dpop.verify_proof(
            request.headers.get("DPoP"),
            method=request.method, path=path,
            access_token=token,
            expected_thumbprint=(claims.get("cnf") or {}).get("jkt"),
        )
    except dpop.DPoPError as exc:
        raise PairingError("device_proof_invalid", exc.detail, status=401) from exc

    device = PairedDevice.objects.filter(id=claims.get("sub")).first()
    if device is None or not device.is_usable:
        # Revocation must bite immediately, even inside a token's 5 minutes.
        raise PairingError("device_revoked", "This device is no longer paired.",
                           status=401)
    if not hmac.compare_digest(str(device.key_thumbprint), str(thumbprint)):
        raise PairingError("device_proof_invalid", "Invalid device proof.",
                           status=401)
    if claims.get("cred_ver") != device.credential_version:
        # A token minted before the last credential rotation or unlink.
        raise PairingError("device_token_stale", "Re-authenticate this device.",
                           status=401)
    return device


# ── pairing lifecycle ─────────────────────────────────────────────────────


def create_pairing(*, jwk, thumbprint, name, platform, arch, app_version):
    """Step 1. Returns `(pairing, device_secret, user_code)`.

    The secret and the code are returned and then forgotten; only keyed
    hashes persist.
    """
    from sessions_app.models import DevicePairing

    secret = new_device_secret()
    expires_at = timezone.now() + timezone.timedelta(
        seconds=DevicePairing.LIFETIME_SECONDS)

    for _ in range(CODE_COLLISION_RETRIES):
        code = new_user_code()
        code_hash = keyed_hash_hex(code)
        if DevicePairing.objects.filter(user_code_hash=code_hash).exists():
            continue  # "code collision retry", per the contract row
        pairing = DevicePairing.objects.create(
            device_secret_hash=keyed_hash(secret),
            user_code_hash=code_hash,
            public_jwk=jwk,
            key_thumbprint=thumbprint,
            name=name, platform=platform, arch=arch, app_version=app_version,
            verification_phrase=new_verification_phrase(),
            expires_at=expires_at,
        )
        return pairing, secret, code

    raise PairingError("pairing_unavailable",
                       "Could not start pairing. Try again.", status=503)


def pairing_from_secret(pairing_id, secret):
    """Load a live pairing whose secret matches, or raise 404/410.

    🔴 A wrong secret and a nonexistent pairing are the same 404, so the id
    space cannot be probed. An *expired* pairing is 410 only once the secret
    has already matched — telling a stranger "that expired" would confirm the
    id existed.
    """
    from sessions_app.models import DevicePairing

    try:
        uuid.UUID(str(pairing_id))
    except (TypeError, ValueError):
        raise PairingError("not_found", "No such pairing.", status=404)

    pairing = DevicePairing.objects.filter(id=pairing_id).first()
    if pairing is None or not secret or not hashes_match(
            secret, pairing.device_secret_hash):
        raise PairingError("not_found", "No such pairing.", status=404)
    if pairing.state == DevicePairing.STATE_FINALIZED:
        raise PairingError("pairing_already_used",
                           "This pairing has already completed.", status=409)
    if pairing.is_expired or pairing.state == DevicePairing.STATE_EXPIRED:
        raise PairingError("pairing_expired",
                           "This pairing expired. Start again.", status=410)
    return pairing


@transaction.atomic
def confirm_pairing(*, user, code, context):
    """Step 2, from the browser. Names the account; grants nothing.

    Returns the pairing. Wrong codes are counted **against the pairing they
    were aimed at**, and five wrong attempts invalidate it.
    """
    from sessions_app.models import DevicePairing

    normalised = normalise_code(code)
    if len(normalised) != CODE_LENGTH:
        raise PairingError("invalid_user_code", "That code is not valid.",
                           status=400)

    pairing = (
        DevicePairing.objects
        .select_for_update()
        .filter(user_code_hash=keyed_hash_hex(normalised))
        .first()
    )
    if pairing is None:
        # No pairing matched. There is nothing to count attempts against —
        # per-account rate limiting is what bounds guessing here, and it is
        # a release-gate item (see the evidence file).
        raise PairingError("invalid_user_code", "That code is not valid.",
                           status=400)

    if pairing.state == DevicePairing.STATE_FINALIZED:
        raise PairingError("pairing_already_used",
                           "This pairing has already completed.", status=409)
    if pairing.is_expired or pairing.state == DevicePairing.STATE_EXPIRED:
        raise PairingError("pairing_expired",
                           "That code expired. Start again on your computer.",
                           status=410)
    if pairing.state == DevicePairing.STATE_BROWSER_CONFIRMED:
        if pairing.account_id == user.id:
            return pairing          # idempotent for the same account
        raise PairingError("pairing_already_claimed",
                           "That code is already in use.", status=409)

    pairing.account = user
    pairing.confirmed_context = context
    pairing.state = DevicePairing.STATE_BROWSER_CONFIRMED
    pairing.confirmed_at = timezone.now()
    pairing.save(update_fields=["account", "confirmed_context", "state",
                                "confirmed_at"])
    return pairing


def register_failed_confirm(code):
    """Count a wrong code against the pairing it named, if it named one.

    Separate from `confirm_pairing` because a *mistyped* code usually names
    no pairing at all, and the attempt budget exists to stop someone walking
    the code space against ONE live pairing.
    """
    from sessions_app.models import DevicePairing

    normalised = normalise_code(code)
    if len(normalised) != CODE_LENGTH:
        return
    with transaction.atomic():
        pairing = (
            DevicePairing.objects.select_for_update()
            .filter(user_code_hash=keyed_hash_hex(normalised)).first()
        )
        if pairing is None or not pairing.is_live:
            return
        pairing.confirm_attempts += 1
        fields = ["confirm_attempts"]
        if pairing.confirm_attempts >= DevicePairing.MAX_CONFIRM_ATTEMPTS:
            pairing.state = DevicePairing.STATE_EXPIRED
            fields.append("state")
        pairing.save(update_fields=fields)


@transaction.atomic
def finalize_pairing(pairing, thumbprint):
    """Step 3. One atomic redemption; returns `(device, access, refresh)`.

    🔴 `select_for_update` plus the state transition inside one transaction
    is what makes redemption single-use: two companions racing the same
    secret cannot both receive credentials, because the loser re-reads a row
    that is already `finalized`.
    """
    from sessions_app.models import DevicePairing, PairedDevice

    pairing = (
        DevicePairing.objects.select_for_update().filter(id=pairing.id).first()
    )
    if pairing is None:
        raise PairingError("not_found", "No such pairing.", status=404)
    if pairing.state == DevicePairing.STATE_FINALIZED:
        raise PairingError("pairing_already_used",
                           "This pairing has already completed.", status=409)
    if pairing.is_expired or pairing.state == DevicePairing.STATE_EXPIRED:
        raise PairingError("pairing_expired",
                           "This pairing expired. Start again.", status=410)
    if pairing.state != DevicePairing.STATE_BROWSER_CONFIRMED:
        # The browser half has not happened. This is the T02 refusal.
        raise PairingError("pairing_not_confirmed",
                           "Confirm this computer in your browser first.",
                           status=409)
    if not hmac.compare_digest(str(pairing.key_thumbprint), str(thumbprint)):
        raise PairingError("device_proof_invalid",
                           "This computer did not start that pairing.",
                           status=403)

    active = PairedDevice.objects.filter(
        owner=pairing.account, status=PairedDevice.STATUS_PAIRED).count()
    if active >= PairedDevice.MAX_PER_ACCOUNT:
        raise PairingError(
            "device_limit_reached",
            f"You can pair at most {PairedDevice.MAX_PER_ACCOUNT} computers. "
            "Unlink one first.",
            status=409)

    existing = PairedDevice.objects.filter(key_thumbprint=thumbprint).first()
    if existing is not None and existing.status != PairedDevice.STATUS_REVOKED:
        # `key_thumbprint` is unique: one key is one device, forever. A key
        # that was revoked must generate a new one rather than return.
        raise PairingError("device_already_paired",
                           "This computer is already paired.", status=409)
    if existing is not None:
        raise PairingError("device_revoked",
                           "This computer was unlinked. Generate a new key.",
                           status=409)

    refresh = new_refresh_token()
    device = PairedDevice.objects.create(
        owner=pairing.account,
        paired_context=pairing.confirmed_context,
        name=pairing.name, platform=pairing.platform, arch=pairing.arch,
        app_version=pairing.app_version,
        public_jwk=pairing.public_jwk,
        key_thumbprint=pairing.key_thumbprint,
        credential_hash=keyed_hash(refresh),
        credential_expires_at=timezone.now() + timezone.timedelta(
            days=REFRESH_TOKEN_TTL_DAYS),
        status=PairedDevice.STATUS_PAIRED,
        paired_at=timezone.now(),
    )

    pairing.state = DevicePairing.STATE_FINALIZED
    pairing.finalized_at = timezone.now()
    pairing.device = device
    pairing.save(update_fields=["state", "finalized_at", "device"])

    return device, mint_access_token(device, thumbprint), refresh


# ══════════════════════════════════════════════════════════════════════════
# RC-007b — credential life: rotation, liveness, revocation
# ══════════════════════════════════════════════════════════════════════════

# PairedDevice.last_seen_at's docstring: "Coarse on purpose — persisted at
# most every 5 minutes. Real liveness is a Redis record with a 6-second TTL;
# this column must never gate a lease."
DEVICE_ONLINE_TTL_SECONDS = 6
LAST_SEEN_PERSIST_SECONDS = 300
_ONLINE_PREFIX = "collab:device:online:"


def mark_online(device):
    """Record liveness in Redis, and persist `last_seen_at` at most every 5
    minutes.

    🔴 The DB write is throttled deliberately. A 10-second heartbeat per
    device that wrote a row every time would put a sustained write load on
    the one box whose `max_connections` is the capacity wall. Redis carries
    the truth; the column is for a human reading the device list.
    """
    from django.core.cache import cache

    try:
        cache.set(f"{_ONLINE_PREFIX}{device.id}", "1",
                  timeout=DEVICE_ONLINE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — liveness is a display concern
        logger.warning("device liveness cache unavailable")

    now = timezone.now()
    if (device.last_seen_at is None
            or (now - device.last_seen_at).total_seconds() >= LAST_SEEN_PERSIST_SECONDS):
        from sessions_app.models import PairedDevice
        PairedDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        device.last_seen_at = now


def is_online(device_id):
    """🔴 Never consult this for authorization. It answers "should the UI say
    online", and a stale or flushed cache must not be able to grant or deny
    anything."""
    from django.core.cache import cache

    try:
        return bool(cache.get(f"{_ONLINE_PREFIX}{device_id}"))
    except Exception:  # noqa: BLE001
        return False


def protocol_compatible(device):
    """Whether this device's protocol range overlaps the server's v1."""
    return (device.protocol_min or 1) <= 1 <= (device.protocol_max or 1)


def rotate_credentials(device_id, refresh_token, thumbprint):
    """Spend a refresh token, mint the next pair, and detect reuse.

    Returns `(device, access_token, refresh_token)`.

    🔴 **Presenting the PREVIOUS refresh token revokes the device.** Two
    parties holding one single-use credential is either a replay or a theft,
    and the two are indistinguishable from here — so this takes the hostile
    reading. That is the standard OAuth refresh-rotation response and it is
    the whole reason `previous_credential_hash` exists on the model.

    🔴 `select_for_update` means two concurrent refreshes cannot both
    succeed: the loser re-reads a row whose current hash has already moved
    on, and is then treated as reuse.

    🔴 **The reuse branch revokes OUTSIDE the transaction, and that is not a
    style choice.** The first version of this function was wrapped in
    `@transaction.atomic` and called `revoke_device` before raising — so the
    raise rolled back the very revocation it was reporting. The caller got
    "this device was unlinked for security" and the device was still paired.
    Caught by `test_reusing_a_spent_refresh_token_REVOKES_the_device`, which
    asserts the row afterwards rather than trusting the status code. Same
    family as this project's `transaction.savepoint()` no-op: **a mutation
    that must survive an error cannot live in the transaction the error
    unwinds.**
    """
    from sessions_app.models import PairedDevice

    reuse_detected = False

    with transaction.atomic():
        device = (
            PairedDevice.objects.select_for_update().filter(id=device_id).first()
        )
        if device is None:
            raise PairingError("device_unknown", "This device is not paired.",
                               status=401)

        if not hmac.compare_digest(str(device.key_thumbprint), str(thumbprint)):
            raise PairingError("device_proof_invalid", "Invalid device proof.",
                               status=401)

        if device.status == PairedDevice.STATUS_REVOKED:
            raise PairingError("device_revoked", "This device was unlinked.",
                               status=401)

        if hashes_match(refresh_token, device.previous_credential_hash):
            # 🔴 Theft, as far as this code is concerned. Flagged here and
            # acted on after the transaction closes — see the docstring.
            reuse_detected = True
        else:
            if not hashes_match(refresh_token, device.credential_hash):
                raise PairingError("credential_invalid",
                                   "Sign this device in again.", status=401)

            if (device.credential_expires_at
                    and timezone.now() >= device.credential_expires_at):
                raise PairingError(
                    "credential_expired",
                    "This device's credential expired. Pair it again.",
                    status=401)

            issued = new_refresh_token()
            device.previous_credential_hash = device.credential_hash
            device.credential_hash = keyed_hash(issued)
            device.credential_expires_at = timezone.now() + timezone.timedelta(
                days=REFRESH_TOKEN_TTL_DAYS)
            # 🔴 Bumping the version invalidates every outstanding access
            # token for this device — `authenticate_device` compares
            # `cred_ver`. Rotation is therefore also a revocation of
            # whatever came before it.
            device.credential_version += 1
            device.save(update_fields=[
                "previous_credential_hash", "credential_hash",
                "credential_expires_at", "credential_version",
            ])

    if reuse_detected:
        revoke_device(device, reason="refresh_reuse_detected")
        raise PairingError(
            "credential_reuse_detected",
            "This device was unlinked for security. Pair it again.",
            status=401)

    return device, mint_access_token(device, thumbprint), issued


@transaction.atomic
def revoke_device(device, *, reason="unlinked"):
    """Unlink a device and terminate everything it holds.

    🔴 Order matters and the contract states it: "revoke bindings/grants
    first". A caller that marked the device revoked and *then* failed partway
    through terminating grants would leave a live grant pointing at a device
    nobody can stop through the normal path.

    🔴 `credential_version` is bumped so outstanding access tokens die inside
    their own 5-minute window — `PairedDevice`'s docstring is explicit that
    holding a valid token must not be sufficient.
    """
    from sessions_app.models import PairedDevice, RemoteControlGrant

    now = timezone.now()

    terminated = (
        RemoteControlGrant.objects
        .filter(device=device, status__in=RemoteControlGrant.NONTERMINAL_STATUSES)
        .update(status=RemoteControlGrant.STATUS_REVOKED, ended_at=now,
                reason=reason)
    )

    PairedDevice.objects.filter(pk=device.pk).update(
        status=PairedDevice.STATUS_REVOKED,
        revoked_at=now,
        credential_hash=None,
        previous_credential_hash=None,
        credential_expires_at=None,
        credential_version=device.credential_version + 1,
    )
    device.refresh_from_db()

    try:
        from django.core.cache import cache
        cache.delete(f"{_ONLINE_PREFIX}{device.id}")
    except Exception:  # noqa: BLE001
        pass

    return terminated
