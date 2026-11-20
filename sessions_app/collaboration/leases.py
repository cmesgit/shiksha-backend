"""Signed activation tickets and bounded leases (RC-008 part 2b).

Spec: ``remote-control-protocol.md`` §Lease and local gate is normative for
every field and every deadline here. Part 2a built the state machine this
sits on; nothing in this module may change a grant's state.

The division of labour is the point of the whole design:

* The **activation ticket** (``shiksha-control+jwt``) says *this pairing of
  controller, target, device, room and capabilities was consented to*. It
  lasts as long as the grant does.
* The **lease** (``shiksha-control-lease+jwt``) says *and it is still true,
  one second ago*. It lasts at most three.

🔴 **The ticket's ``exp`` is not a permission to inject.** The protocol says
so in as many words — "``exp`` is grant hard expiry, **not** a replacement
for a current lease". A companion holding a valid ticket and no fresh lease
must keep the gate shut. If that ever inverts, revocation latency becomes
the ticket lifetime (ten minutes) instead of the lease deadline (three
seconds), which is the difference between this feature being safe and not.

🔴 **A lease is a DB read plus Redis. Never a row write.** At one lease per
second per active grant, writing a row per renewal puts the write rate at
the number of concurrent controls, and the contract is explicit that the box
dies around twenty. ``lease_seq`` therefore lives in the cache, and the only
database work is a ``SELECT`` under a device-row lock.

🔴 **Every renewal re-reads authority from the database.** Not from the
ticket, not from a cached decision, not from channel-group membership. The
protocol lists what must be rechecked each time — grant state, version,
epoch, device revocation, binding freshness, the kill flag — because a
worker holding a pre-revoke decision is exactly how a revoked grant keeps
injecting.

🔴 **Never log a ticket or a lease, and never put either in LiveKit room
metadata.** Room metadata is readable by every participant.
"""
import base64
import hashlib
import logging
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import bindings as binding_store
from . import grants as grant_svc
from .grants import GrantError

logger = logging.getLogger(__name__)

# ── Normative constants ───────────────────────────────────────────────────
PROTOCOL_VERSION = 1
ALGORITHM = "EdDSA"
ACTIVATION_TYP = "shiksha-control+jwt"
LEASE_TYP = "shiksha-control-lease+jwt"
AUDIENCE = "shiksha-remote-companion"

#: "exp ≤ iat + 3s". The companion's own deadline is
#: min(request_start + 3s, this), measured on a monotonic clock — never
#: receive_time + 3s, which a slow response would silently extend.
LEASE_TTL_SECONDS = 3
#: "device-authenticated every 1s", "1/s target, max 2/s".
LEASE_INTERVAL_SECONDS = 1
LEASE_MAX_PER_SECOND = 2

_SEQ_PREFIX = "collab:lease:seq:"
_RATE_PREFIX = "collab:lease:rate:"
#: A grant's leases outlive its hard expiry by nothing; keep the counter a
#: little longer so a late renewal cannot reuse a sequence number.
_SEQ_TTL_SECONDS = 15 * 60


# ── Signing key ───────────────────────────────────────────────────────────


def _signing_key():
    """The Ed25519 private key control tickets are signed with.

    Configured as a PEM in ``COLLABORATION_CONTROL_SIGNING_KEY``. Unset, it
    is **derived** from ``SECRET_KEY`` — same philosophy as
    ``devices._server_key``: a deployment that forgets to set it still does
    not reuse the session-signing secret verbatim, and still gets a stable
    key across restarts so outstanding tickets survive a redeploy.

    🔴 Derivation is a development convenience, not a production posture.
    ``deployment-rollout.md`` requires a separate key for control tokens;
    rotating ``SECRET_KEY`` would silently invalidate every live grant.
    """
    configured = getattr(settings, "COLLABORATION_CONTROL_SIGNING_KEY", "") or ""
    if configured:
        return serialization.load_pem_private_key(
            configured.encode() if isinstance(configured, str) else configured,
            password=None,
        )
    seed = hashlib.sha256(
        b"shiksha-collaboration-control-v1:" + settings.SECRET_KEY.encode()
    ).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_key():
    key = _signing_key()
    if isinstance(key, Ed25519PublicKey):
        return key
    return key.public_key()


def key_id():
    """A stable ``kid`` so an unknown key can be *rejected*, not guessed at.

    The protocol requires rejecting unknown ``kid``; that is only meaningful
    if tickets carry one. Derived from the public key, so rotating the key
    rotates the id automatically and a ticket signed by the old key is
    identifiable rather than merely invalid.
    """
    raw = _public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()[:16]).decode(
    ).rstrip("=")


def public_key_pem():
    """What a companion pins. Safe to publish; safe to log."""
    return _public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _issuer():
    return getattr(settings, "COLLABORATION_CANONICAL_URL", "") or "shiksha"


# ── Activation ticket ─────────────────────────────────────────────────────


def mint_activation(grant, *, device, display_id=None):
    """The long-lived half: who consented to what, signed.

    Called only with a grant the state machine has just moved to ``active``.
    Every field is read from the row, never from a request body — the ticket
    must describe what the server decided, not what a caller asserted.
    """
    if grant.status != grant_svc.ACTIVE:
        raise GrantError("invalid_state",
                         "Only an active grant has an activation ticket.",
                         status=409)
    if grant.epoch is None or grant.expires_at is None:
        # Unreachable through the machine; a loud failure beats a ticket with
        # a null epoch, which would validate against any epoch at all.
        raise GrantError("invalid_state", "This grant is not fully activated.",
                         status=409)

    descriptor = grant.display_descriptor
    resolved_display = display_id
    if resolved_display is None and isinstance(descriptor, dict):
        resolved_display = descriptor.get("display_id")

    now = int(timezone.now().timestamp())
    claims = {
        "iss": _issuer(),
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
        "protocol": PROTOCOL_VERSION,

        "grant_id": str(grant.public_id),
        "grant_version": grant.version,
        "epoch": str(grant.epoch),

        "session": grant.session_ref(),
        "room_name": grant.room_name,

        "controller_user_id": str(grant.controller_id),
        "target_user_id": str(grant.target_id),
        "controller_context": grant.controller_context,
        "target_context": grant.target_context,

        "controller_identity": grant.controller_identity,
        "controller_sid": grant.controller_sid,
        "host_identity": grant.host_identity,
        "host_sid": grant.host_sid,
        "companion_identity": grant.companion_identity,
        "companion_sid": grant.companion_sid,

        "device_id": str(device.id),
        "device_thumbprint": str(device.key_thumbprint),
        "host_binding_id": str(grant.host_binding_id or ""),
        "controller_binding_id": str(grant.controller_binding_id or ""),

        "capabilities": list(grant.capabilities or []),
        "capture_generation": grant.capture_generation,
        "track_sid": grant.track_sid,
        "display_id": resolved_display,

        "created_at": now,
        "iat": now,
        "nbf": now,
        # 🔴 The grant's hard expiry, and NOT a licence to inject until then.
        "exp": int(grant.expires_at.timestamp()),
    }
    return jwt.encode(
        claims, _signing_key(), algorithm=ALGORITHM,
        headers={"typ": ACTIVATION_TYP, "kid": key_id()},
    )


def activation_hash(activation_jwt):
    """What a lease binds itself to, so a lease cannot be moved to another
    ticket for the same grant."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(activation_jwt.encode()).digest()
    ).decode().rstrip("=")


def verify_activation(token, *, expected_grant_id=None, expected_epoch=None):
    """Decode and structurally validate a ticket.

    Used by the tests and available to any future verifier. The algorithm
    list is fixed, ``typ`` is checked explicitly and ``kid`` must match the
    current key — the protocol requires rejecting unknown algorithms and
    unknown key ids, and PyJWT will happily accept a token whose header says
    something else if you let it.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise GrantError("activation_invalid", "Invalid activation ticket.",
                         status=403) from exc
    if header.get("typ") != ACTIVATION_TYP:
        raise GrantError("activation_invalid", "Invalid activation ticket.",
                         status=403)
    if header.get("kid") != key_id():
        raise GrantError("activation_unknown_key",
                         "Unknown signing key.", status=403)
    try:
        claims = jwt.decode(
            token, _public_key(), algorithms=[ALGORITHM], audience=AUDIENCE,
            options={"require": ["exp", "nbf", "iat", "aud", "grant_id",
                                 "epoch", "grant_version"]},
        )
    except jwt.PyJWTError as exc:
        raise GrantError("activation_invalid", "Invalid activation ticket.",
                         status=403) from exc

    if claims.get("protocol") != PROTOCOL_VERSION:
        raise GrantError("activation_invalid", "Unsupported protocol version.",
                         status=403)
    if expected_grant_id and claims.get("grant_id") != str(expected_grant_id):
        raise GrantError("activation_invalid", "Ticket is for another grant.",
                         status=403)
    if expected_epoch and claims.get("epoch") != str(expected_epoch):
        raise GrantError("activation_stale_epoch",
                         "Ticket is for a previous epoch.", status=409)
    return claims


# ── Leases ────────────────────────────────────────────────────────────────


def _next_seq(grant_public_id):
    """Monotonic per-grant lease counter, in the cache.

    🔴 Cache, not a column: a column would make this a row write every
    second per active control. ``incr`` is atomic on the Redis backend, which
    is what stops two workers minting the same ``lease_seq`` for one grant —
    and a duplicate seq is not cosmetic, because the companion's rule is
    "reject lower *or equal* lease_seq", so a duplicate would make the
    companion discard a legitimate renewal and pause.
    """
    key = f"{_SEQ_PREFIX}{grant_public_id}"
    try:
        return cache.incr(key)
    except ValueError:
        # Key absent. add() is atomic, so a racing worker either wins here
        # and we incr on top of it, or we win and it incrs on top of us.
        if cache.add(key, 1, timeout=_SEQ_TTL_SECONDS):
            return 1
        return cache.incr(key)


def _check_rate(grant_public_id):
    """At most two leases per second for one grant.

    The contract's "1/s target, max 2/s" is a ceiling on a renewal loop, not
    a security boundary — the security boundary is the 3s deadline. It is
    enforced anyway, because a companion spinning on this endpoint is either
    broken or probing, and both deserve a 429 rather than service.
    """
    window = int(time.time())
    key = f"{_RATE_PREFIX}{grant_public_id}:{window}"
    try:
        if cache.add(key, 1, timeout=2):
            return
        count = cache.incr(key)
    except ValueError:
        return
    if count > LEASE_MAX_PER_SECOND:
        raise GrantError("rate_limited", "Slow down.", status=429)


def mint_lease(grant_public_id, *, device, epoch, expected_version, nonce,
               activation_jwt=None):
    """The short-lived half: *and it is still true*.

    Re-reads every piece of authority from the database under a device-row
    lock, then signs a three-second assertion. Returns
    ``(lease_jwt, grant)``.

    🔴 The device lock is what the contract means by "per-device
    serialization locks prevent a lease from committing after revoke". It is
    a ``SELECT ... FOR UPDATE`` — a lock, not a write — so the no-row-write
    rule holds. A revoke running concurrently either lands before this read
    (and we refuse) or waits behind it (and the lease it raced is already
    bounded to three seconds).
    """
    from sessions_app.models import PairedDevice, RemoteControlGrant

    if not nonce or not isinstance(nonce, str) or len(nonce) < 16:
        # The nonce is what ties this reply to the request still outstanding
        # on the companion. A short or absent one is a protocol error.
        raise GrantError("lease_nonce_required",
                         "A fresh lease nonce is required.", status=400)

    _check_rate(grant_public_id)

    # 🔴 Re-read the kill flag per renewal, never from the ticket.
    # "Global flag-off invalidates new leases."
    grant_svc.assert_control_enabled()

    with transaction.atomic():
        PairedDevice.objects.select_for_update().filter(pk=device.pk).first()
        grant = (
            RemoteControlGrant.objects
            .filter(public_id=grant_public_id,
                    schema_version=RemoteControlGrant.SCHEMA_V1)
            .first()
        )
        if grant is None:
            raise GrantError("not_found", "No such grant.", status=404)
        if grant.device_id != device.id:
            raise GrantError("not_found", "No such grant.", status=404)

        device.refresh_from_db()
        if not device.is_usable:
            raise GrantError("device_revoked",
                             "This device is no longer paired.", status=409)

        if grant.status != grant_svc.ACTIVE:
            # 409 non-active, per the contract's lease row.
            raise GrantError("not_active",
                             f"This grant is {grant.status}.", status=409)
        if grant.expires_at is None or grant.expires_at <= timezone.now():
            raise GrantError("expired", "This grant has expired.", status=410)
        if str(grant.epoch) != str(epoch):
            raise GrantError("stale_epoch",
                             "This epoch is no longer current.", status=409)
        if int(expected_version) != grant.version:
            raise GrantError("version_conflict",
                             "This grant changed since you last read it.",
                             status=409)

        _assert_bindings_fresh(grant)

    seq = _next_seq(grant_public_id)
    now = int(timezone.now().timestamp())
    claims = {
        "iss": _issuer(),
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
        "protocol": PROTOCOL_VERSION,
        "grant_id": str(grant.public_id),
        "grant_version": grant.version,
        "epoch": str(grant.epoch),
        "device_id": str(device.id),
        "lease_seq": seq,
        "nonce": nonce,
        "iat": now,
        "nbf": now,
        "exp": now + LEASE_TTL_SECONDS,
    }
    if activation_jwt:
        claims["activation_hash"] = activation_hash(activation_jwt)

    token = jwt.encode(
        claims, _signing_key(), algorithm=ALGORITHM,
        headers={"typ": LEASE_TYP, "kid": key_id()},
    )
    return token, grant


def _assert_bindings_fresh(grant):
    """Both ends must have proved they still exist within the last 3s.

    🔴 "Browser heartbeat proves this UI still exists; it is not alone proof
    of LiveKit membership." It is still necessary: a controller whose tab
    closed stops heartbeating, and three seconds later its leases stop.

    The host side is checked only when there is something to check. Host
    bindings are RC-009's, and until they exist this is a documented hole,
    not a silent one: a grant whose *host* vanished is currently caught by
    the binding-deletion cascade and by LiveKit SID verification, neither of
    which is the 3s bound the protocol specifies. RC-009 must close it.
    """
    from . import host_bindings as host_store

    checks = (
        (grant.controller_binding_id, "controller", binding_store),
        (grant.host_binding_id, "host", host_store),
    )
    for binding_id, who, store in checks:
        if not binding_id:
            continue
        try:
            record = store.get(str(binding_id))
        except store.StoreUnavailable:
            # "Backend dependencies unavailable means no renewal." Failing
            # open here would keep input flowing during exactly the outage
            # that stops us detecting a disconnect.
            raise GrantError("dependency_unavailable",
                             "Collaboration is temporarily unavailable.",
                             status=503)
        if record is None:
            raise GrantError("binding_lapsed",
                             f"The {who} is no longer connected.", status=409)
        if who == "host" and record.get("state") != host_store.STATE_READY:
            # Sharing stopped, or never finished starting. Either way there
            # is no live capture for this grant to be driving.
            raise GrantError("capture_lost",
                             "The shared display is no longer live.",
                             status=409)
        if not store.is_fresh_for_lease(record):
            raise GrantError("binding_stale",
                             f"The {who} connection is stale.", status=409)


def verify_lease(token, *, expected_grant_id=None, expected_epoch=None,
                 expected_nonce=None, now=None):
    """Decode a lease and enforce its deadline. Used by the tests.

    The companion's real check is stricter and cannot live here: its
    deadline is ``min(request_start + 3s, exp)`` on a **monotonic** clock,
    measured from when it sent the request, so a slow reply shortens the
    window rather than extending it. A server-side verifier can only see
    ``exp``.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise GrantError("lease_invalid", "Invalid lease.", status=403) from exc
    if header.get("typ") != LEASE_TYP:
        raise GrantError("lease_invalid", "Invalid lease.", status=403)
    if header.get("kid") != key_id():
        raise GrantError("lease_unknown_key", "Unknown signing key.",
                         status=403)
    try:
        claims = jwt.decode(
            token, _public_key(), algorithms=[ALGORITHM], audience=AUDIENCE,
            options={"require": ["exp", "iat", "grant_id", "epoch",
                                 "lease_seq", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise GrantError("lease_invalid", "Invalid lease.", status=403) from exc

    if claims["exp"] - claims["iat"] > LEASE_TTL_SECONDS:
        # A lease longer than the deadline is a forged or buggy one; the
        # bound is the entire safety property.
        raise GrantError("lease_invalid", "Lease exceeds the deadline.",
                         status=403)
    if expected_grant_id and claims.get("grant_id") != str(expected_grant_id):
        raise GrantError("lease_invalid", "Lease is for another grant.",
                         status=403)
    if expected_epoch and claims.get("epoch") != str(expected_epoch):
        raise GrantError("stale_epoch", "Lease is for a previous epoch.",
                         status=409)
    if expected_nonce is not None and claims.get("nonce") != expected_nonce:
        # "Only a response to the currently outstanding nonce can renew."
        raise GrantError("lease_nonce_mismatch",
                         "Lease does not answer the outstanding nonce.",
                         status=403)
    return claims
