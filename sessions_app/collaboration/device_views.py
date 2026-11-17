"""The pairing routes (RC-007a).

    POST pairings/               anonymous + DPoP proof of a new key
    POST pairings/confirm/       B, recent account auth ≤10min
    POST pairings/<id>/poll/     P  (pairing secret, in Authorization)
    POST pairings/<id>/finalize/ P  + native local confirmation

Two different authentication planes meet here, and conflating them is the
bug this file exists to avoid:

* **B** routes are browser routes: cookie auth, explicit CSRF, exact Origin.
* **D/P** routes are device routes. `api-contracts.md`: *"new device routes
  accept device-scoped DPoP authentication only, never user cookies as
  substitute"*. They therefore do **not** run the CSRF/Origin boundary —
  a companion is not a browser and has no Origin — and they must never
  consult `request.user`.

🔴 **`device_endpoint` is not `collaboration_endpoint` with bits removed.**
It is a separate boundary that keeps the feature flag, the error envelope
and `Cache-Control: no-store`, and deliberately omits the browser checks. If
you ever find yourself adding a cookie fallback to one of these, stop: that
is the substitution the contract forbids.

The device management routes (`devices/`, `device-token/`,
`device-heartbeat/`) are RC-007b.
"""
import functools
import uuid

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from . import devices as device_svc
from . import dpop
from . import policy
from .auth import collaboration_endpoint, error, ok, parse_body
from .devices import PairingError
from .policy import PolicyError

# "B, recent account auth ≤10min" on confirm and unlink.
RECENT_AUTH_SECONDS = 600


def device_endpoint(view):
    """The device-plane boundary: flag gate, error envelope, no browser checks."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        request_id = str(uuid.uuid4())
        try:
            if not policy.feature_enabled():
                return error("not_found", "Not found.", status=404,
                             request_id=request_id)
            return view(request, *args, **kwargs)
        except PolicyError as exc:
            return error(exc.code, exc.detail, status=exc.status,
                         request_id=request_id)

    return wrapper


def _pairing_secret(request):
    """The pairing secret, from Authorization only.

    🔴 Never from the URL or the body. `api-contracts.md`: *"secret in
    Authorization, not URL/body logs"* — a path segment lands in every proxy
    access log, and a body lands in request-logging middleware.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Pairing "):
        raise PairingError("pairing_auth_required",
                           "Pairing credential required.", status=401)
    return header[len("Pairing "):].strip()


def _recent_auth_at(request, principal):
    """When this browser session actually signed in.

    Read from `UserSession.created_at` for the token's `sid` — the row is
    created by LoginView and `last_active_at` is bumped by every token
    rotation, so `created_at` is the only field that still means *login*.

    ⚠️ **This measures "signed in within 10 minutes", not "re-entered a
    password within 10 minutes".** The platform has no step-up re-auth
    prompt, so a true re-authentication cannot be demanded yet; RC-011 owns
    the UI that would. Recorded as a known weakening of the contract row
    rather than silently treated as equivalent.
    """
    from accounts.settings_models import UserSession

    row = (
        UserSession.objects
        .filter(user=principal.user, id=principal.auth_sid, revoked_at__isnull=True)
        .first()
    )
    return row.created_at if row else None


def _require_recent_auth(request, principal):
    signed_in_at = _recent_auth_at(request, principal)
    if signed_in_at is None:
        raise PolicyError(
            "auth_session_required",
            "Sign in again to pair a computer.", status=403)
    age = (timezone.now() - signed_in_at).total_seconds()
    if age > RECENT_AUTH_SECONDS:
        raise PolicyError(
            "recent_auth_required",
            "For security, sign in again before pairing a computer.",
            status=403)


# ── POST pairings/ ────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def create_pairing(request):
    """Step 1. A companion introduces a fresh key and gets a code.

    Anonymous by design: no account is named here, and none can be. The DPoP
    proof is *self-asserted* — the key is being introduced, so there is
    nothing to pin it against yet — which is exactly why this step grants
    nothing beyond the right to be confirmed by a signed-in human.
    """
    body = parse_body(
        request,
        required=("name", "platform", "arch", "public_jwk"),
        optional=("app_version",),
    )

    jwk = body.get("public_jwk")
    if not isinstance(jwk, dict):
        raise PairingError("invalid_public_jwk", "public_jwk must be a JWK.",
                           status=400)

    try:
        thumbprint, _ = dpop.verify_proof(
            request.headers.get("DPoP"), method="POST", path="pairings/")
    except dpop.DPoPError as exc:
        raise PairingError("device_proof_invalid", exc.detail, status=401)

    # The proof's key and the registered key must be the same key — otherwise
    # a companion could prove one key and register somebody else's.
    if dpop.jwk_thumbprint(jwk) != thumbprint:
        raise PairingError("device_proof_invalid",
                           "The proof does not match the submitted key.",
                           status=401)

    for field, limit in (("name", 80), ("platform", 32), ("arch", 16)):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PairingError(f"invalid_{field}", f"{field} is required.",
                               status=400)
        if len(value) > limit:
            raise PairingError(f"invalid_{field}", f"{field} is too long.",
                               status=400)

    pairing, secret, code = device_svc.create_pairing(
        jwk=jwk,
        thumbprint=thumbprint,
        name=body["name"].strip(),
        platform=body["platform"].strip(),
        arch=body["arch"].strip(),
        app_version=(body.get("app_version") or "")[:32],
    )

    # 🔴 The only time either value exists outside the companion's memory.
    return ok({
        "pairing_id": str(pairing.id),
        "device_secret": secret,
        "user_code": device_svc.format_code(code),
        "expires_at": pairing.expires_at.isoformat(),
        "poll_interval_s": pairing.POLL_INTERVAL_SECONDS,
    }, status=201)


# ── POST pairings/confirm/ ────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def confirm_pairing(request):
    """Step 2, from a signed-in browser. Names the account; grants nothing.

    🔴 This route cannot complete a pairing, and that is the T02 property:
    an attacker holding a stolen browser session can reach here and still
    gets no credentials, because the companion must separately confirm
    locally and redeem a secret this response never contains.
    """
    principal = policy.principal_from_request(request)
    _require_recent_auth(request, principal)
    body = parse_body(request, required=("user_code",))

    code = body.get("user_code")
    try:
        pairing = device_svc.confirm_pairing(
            user=principal.user,
            code=code,
            context={
                "context": principal.context,
                "profile_id": principal.profile_id,
                "auth_session_id": principal.auth_sid,
            },
        )
    except PairingError:
        # Count the miss against the pairing it named, if any, then re-raise
        # unchanged — the caller must not learn whether a pairing existed.
        device_svc.register_failed_confirm(code)
        raise

    return ok({
        "pairing_id": str(pairing.id),
        "device_name": pairing.name,
        "platform": pairing.platform,
        "key_fingerprint": pairing.key_thumbprint,
        "verification_phrase": pairing.verification_phrase,
        "state": pairing.state,
    })


# ── POST pairings/<id>/poll/ ──────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def poll_pairing(request, pairing_id):
    """Step 2.5. The companion waits, holding the secret.

    🔴 The account is revealed **only** to a caller whose secret matches —
    "revealing account only after matching secret/key". A guessed pairing id
    discloses nothing, because a wrong secret is an indistinguishable 404.
    """
    parse_body(request)  # must be an empty object; unknown fields rejected
    secret = _pairing_secret(request)
    pairing = device_svc.pairing_from_secret(pairing_id, secret)

    payload = {
        "state": pairing.state,
        "expires_at": pairing.expires_at.isoformat(),
    }
    if pairing.state == pairing.STATE_BROWSER_CONFIRMED and pairing.account:
        payload["account_display_name"] = (
            pairing.account.get_full_name() or pairing.account.username
        )
        payload["verification_phrase"] = pairing.verification_phrase

    pairing.last_polled_at = timezone.now()
    pairing.save(update_fields=["last_polled_at"])
    return ok(payload)


# ── POST pairings/<id>/finalize/ ──────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def finalize_pairing(request, pairing_id):
    """Step 3. Local confirmation on the desktop redeems the secret.

    Requires all three independently: the pairing secret, a DPoP proof from
    the *same* key that started the pairing, and `local_confirm: true`
    standing for a physical action on the machine being paired.

    🔴 `local_confirm` is a claim the companion makes, not something the
    server can verify — that is stated plainly in the threat model rather
    than dressed up. Its value is that an attacker who has the secret but no
    code execution on the desktop cannot produce it.
    """
    body = parse_body(request, required=("local_confirm",))
    if body.get("local_confirm") is not True:
        raise PairingError("local_confirm_required",
                           "Confirm the pairing on your computer.", status=403)

    secret = _pairing_secret(request)
    pairing = device_svc.pairing_from_secret(pairing_id, secret)

    try:
        thumbprint, _ = dpop.verify_proof(
            request.headers.get("DPoP"),
            method="POST", path=f"pairings/{pairing_id}/finalize/",
            expected_thumbprint=pairing.key_thumbprint,
        )
    except dpop.DPoPError as exc:
        raise PairingError("device_proof_invalid", exc.detail, status=401)

    device, access_token, refresh_token = device_svc.finalize_pairing(
        pairing, thumbprint)

    return ok({
        "device_id": str(device.id),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": device_svc.ACCESS_TOKEN_TTL_SECONDS,
    })
