"""The control-grant routes (RC-008 part 2d).

``api-contracts.md`` §Control grants is the frozen surface; this file is its
HTTP half and nothing more. Every authorization decision lives in
``grants`` or ``leases`` — a view here parses, authenticates the *plane*,
delegates, and serialises. Nothing may decide here that the services do not
already decide, or the two will drift and the tests that matter will be
testing the wrong layer.

    POST   grants/                  B  request control
    GET    grants/{id}/             B|D read; D also gets a consent nonce
    POST   grants/{id}/decision/    B  the target allows or declines
    POST   grants/{id}/activate/    D  physical consent -> ticket + lease
    POST   grants/{id}/lease/       D  renew, every 1s
    POST   grants/{id}/pause/       B|D
    POST   grants/{id}/resume-intent/ B  target asks for it back
    POST   grants/{id}/resume/      D  physical re-approval
    POST   grants/{id}/terminate/   B|D stop

🔴 **Two authentication planes, never interchangeable.** Browser routes take
cookie auth plus CSRF plus an exact Origin (``collaboration_endpoint``).
Device routes take a device access token plus a DPoP proof and **no cookie
is ever consulted** (``device_endpoint`` + ``authenticate_device``). A
device credential must not be able to drive a browser route and a logged-in
user must not be able to drive a device one — that separation is what makes
"only the companion can activate" true rather than aspirational.

🔴 **The lease route is deliberately not idempotency-cached.** Every other
mutation requires an ``Idempotency-Key`` and replays the stored result; a
lease must not, because replaying a cached lease is precisely the thing a
three-second deadline exists to prevent. The contract says so in the lease
row: *"not idempotency-cached like mutations"*.

🔴 **A lease is never returned to a browser.** ``activate`` and ``resume``
answer a device, and only a device. ``api-contracts.md``: *"Do not return
device credential, signed activation, lease or other participant context
secrets to room viewers."*
"""
import uuid

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from . import devices as device_svc
from . import grants as grant_svc
from . import leases as lease_svc
from . import policy
from .auth import collaboration_endpoint, error, idempotency_guard, ok, parse_body
from .device_views import device_endpoint
from .policy import PolicyError


# ── Serialisation ─────────────────────────────────────────────────────────


def _person(user):
    if user is None:
        return None
    name = (user.get_full_name() or "").strip() or user.username
    return {"user_id": str(user.id), "display_name": name}


def serialize_grant(grant):
    """The contract's ``Grant`` shape.

    🔴 What is deliberately absent: the consent-nonce hash, the activation
    ticket, any lease, the device credential and both auth-session ids. The
    context snapshots are summarised to a bare context name — a profile id
    and a tracked session id are participant secrets, not grant metadata.
    """
    return {
        "id": str(grant.public_id),
        "version": grant.version,
        "state": grant.status,
        "session": grant.session_ref(),
        "controller": _person(grant.controller),
        "target": _person(grant.target),
        "device": ({"id": str(grant.device_id), "name": grant.device.name}
                   if grant.device_id else None),
        "capabilities": list(grant.capabilities or []),
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "request_expires_at": (grant.request_expires_at.isoformat()
                               if grant.request_expires_at else None),
        "approval_expires_at": (grant.approval_expires_at.isoformat()
                                if grant.approval_expires_at else None),
        "reason": grant.reason or None,
        "epoch": str(grant.epoch) if grant.epoch else None,
        "capture_generation": grant.capture_generation,
        "track_sid": grant.track_sid or None,
    }


# ── Shared lookups ────────────────────────────────────────────────────────


def _load_for_browser(public_id, principal):
    """A grant this browser principal is party to, or 404.

    404 and not 403, per the contract's "nonexistent OR inaccessible": a
    stranger must not be able to tell a real grant id from a fabricated one.
    """
    from sessions_app.models import RemoteControlGrant

    try:
        uuid.UUID(str(public_id))
    except (TypeError, ValueError):
        raise PolicyError("not_found", "No such grant.", status=404)

    grant = (RemoteControlGrant.objects
             .filter(public_id=public_id,
                     schema_version=RemoteControlGrant.SCHEMA_V1)
             .select_related("controller", "target", "device").first())
    if grant is None:
        raise PolicyError("not_found", "No such grant.", status=404)
    if principal.user.id not in (grant.controller_id, grant.target_id):
        raise PolicyError("not_found", "No such grant.", status=404)
    return grant


def _load_for_device(public_id, device):
    from sessions_app.models import RemoteControlGrant

    try:
        uuid.UUID(str(public_id))
    except (TypeError, ValueError):
        raise PolicyError("not_found", "No such grant.", status=404)

    grant = (RemoteControlGrant.objects
             .filter(public_id=public_id,
                     schema_version=RemoteControlGrant.SCHEMA_V1,
                     device=device)
             .select_related("controller", "target", "device").first())
    if grant is None:
        raise PolicyError("not_found", "No such grant.", status=404)
    return grant


def _reload(grant):
    grant.refresh_from_db()
    return grant


def _route(name):
    """Idempotency keys are scoped per route, so the same key on two
    different actions is two different operations."""
    return f"grants:{name}"


# ── POST grants/ ──────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def create_grant(request):
    """Ask a participant for control of their desktop.

    🔴 The controller and target are derived server-side — the controller
    from the authenticated principal, the target from the *host binding's*
    owner. The contract is explicit: "no client controller/target IDs".
    A body that could name its own target would let anyone push a consent
    prompt at anyone.
    """
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, _route("create"), principal)
    if replay is not None:
        return replay

    body = parse_body(request, required=("controller_binding_id",
                                         "host_binding_id", "capabilities"))

    controller_binding_id = str(body["controller_binding_id"])
    record = _require_own_binding(controller_binding_id, principal)
    session = policy.resolve_session(record.get("session_kind"),
                                     record.get("session_id"))

    target_user, device = _resolve_host_binding(body["host_binding_id"],
                                                session)

    grant = grant_svc.request_control(
        principal=principal,
        session=session,
        device=device,
        target_user=target_user,
        controller_binding_id=controller_binding_id,
        host_binding_id=str(body["host_binding_id"]),
        capabilities=body["capabilities"],
        room_name=record.get("room_name") or "",
        controller_identity=record.get("identity") or "",
        controller_sid=record.get("participant_sid") or "",
    )
    return commit(ok(serialize_grant(grant), status=201))


def _require_own_binding(binding_id, principal):
    from . import bindings as binding_store

    try:
        record = binding_store.get(str(binding_id))
    except binding_store.StoreUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Collaboration is temporarily unavailable.",
                          status=503)
    if record is None or not binding_store.owned_by(record, principal):
        raise PolicyError("not_found", "No such binding.", status=404)
    return record


def _resolve_host_binding(host_binding_id, session):
    """Who is being asked, and on which device — derived, never supplied.

    Filled in by RC-009a. Both the target and the device come from the host
    binding's own record, so a request body cannot name either: that is the
    contract's "no client controller/target IDs", and without it anyone
    could push a consent prompt at anyone.

    Four gates, and each one refuses differently on purpose:

    * the share must exist and be **ready** — an intent that never reached a
      published track is not something to ask about;
    * it must be in **this room** — a share in another session is not
      visible here and must not be reachable by id;
    * the owner must have **enabled assistance** — sharing a screen is not
      offering it for control, and `allow_requests` defaults False;
    * the device must still be **paired**.
    """
    from sessions_app.models import PairedDevice
    from . import host_bindings as host_store

    try:
        record = host_store.get(str(host_binding_id))
    except host_store.StoreUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Collaboration is temporarily unavailable.",
                          status=503)
    if record is None or record.get("state") in host_store.TERMINAL_STATES:
        raise PolicyError("capture_not_ready",
                          "The owner must select and share a display in the "
                          "companion.", status=409)
    if record.get("session_id") != str(session.id):
        # 404, not 403: a share in another room must not be distinguishable
        # from one that does not exist.
        raise PolicyError("not_found", "No such share.", status=404)
    if record.get("state") != host_store.STATE_READY:
        raise PolicyError("capture_not_ready",
                          "That display is not being shared yet.", status=409)
    if not record.get("allow_requests"):
        raise PolicyError("requests_disabled",
                          "That participant has not enabled assistance.",
                          status=403)

    device = PairedDevice.objects.filter(id=record.get("device_id")).first()
    if device is None or not device.is_usable:
        raise PolicyError("device_revoked", "That device is not paired.",
                          status=409)

    from accounts.models import User
    target = User.objects.filter(id=record.get("owner_user_id")).first()
    if target is None:
        raise PolicyError("not_found", "No such share.", status=404)
    return target, device


# ── GET grants/{id}/ ──────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([AllowAny])
def read_grant(request, grant_id):
    """Readable by either plane; the device additionally gets a nonce.

    Dispatches on the credential actually presented rather than taking a
    parameter, so a caller cannot choose which plane validates it. A DPoP
    Authorization header means the device plane and nothing else.
    """
    if request.headers.get("Authorization", "").startswith("DPoP "):
        return _read_grant_as_device(request, grant_id)
    return _read_grant_as_browser(request, grant_id)


@collaboration_endpoint(unsafe=False)
def _read_grant_as_browser(request, grant_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    grant = _load_for_browser(grant_id, principal)
    return ok(serialize_grant(grant))


@device_endpoint
def _read_grant_as_device(request, grant_id):
    device = device_svc.authenticate_device(
        request, path=f"grants/{grant_id}/")
    grant = _load_for_device(grant_id, device)
    payload = serialize_grant(grant)

    # "D additionally gets short-lived consent_nonce when approved". Minting
    # it on read is what lets the companion show its dialog and activate in
    # one pass; it is single-use, so a second read invalidates the first.
    if grant.status in (grant_svc.APPROVED, grant_svc.PAUSED):
        try:
            payload["consent_nonce"] = grant_svc.issue_consent_nonce(
                grant_id, device=device)
        except grant_svc.GrantError:
            # Not consent-ready (e.g. paused with no resume intent). The
            # grant itself is still readable; the nonce simply is not there.
            payload["consent_nonce"] = None
    else:
        payload["consent_nonce"] = None
    return ok(payload)


# ── POST grants/{id}/decision/ ────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def decide_grant(request, grant_id):
    """The target allows or declines. Never reaches ``active``."""
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, _route("decision"), principal)
    if replay is not None:
        return replay

    body = parse_body(request, required=("allow", "capabilities",
                                         "expected_version"))
    if not isinstance(body["allow"], bool):
        raise PolicyError("invalid_body", "allow must be a boolean.",
                          status=400)

    grant = grant_svc.decide(
        grant_id, principal=principal, allow=body["allow"],
        capabilities=body["capabilities"],
        expected_version=body["expected_version"],
    )
    return commit(ok(serialize_grant(grant)))


# ── POST grants/{id}/activate/ and /resume/ ───────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def activate_grant(request, grant_id):
    """Physical consent on the device. The only route that opens a gate."""
    return _activate(request, grant_id, path_name="activate")


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def resume_grant(request, grant_id):
    """Re-approval after a pause. Same transition, same requirements —
    a fresh epoch and a fresh physical confirmation, never a reconnect."""
    return _activate(request, grant_id, path_name="resume")


def _activate(request, grant_id, *, path_name):
    device = device_svc.authenticate_device(
        request, path=f"grants/{grant_id}/{path_name}/")
    body = parse_body(request, required=("consent_nonce", "expected_version",
                                         "capture_generation", "local_confirm",
                                         "lease_nonce"))

    grant = grant_svc.activate(
        grant_id, device=device,
        consent_nonce=body["consent_nonce"],
        expected_version=body["expected_version"],
        capture_generation=body["capture_generation"],
        local_confirm=body["local_confirm"],
    )

    activation_jwt = lease_svc.mint_activation(grant, device=device)
    lease_jwt, grant = lease_svc.mint_lease(
        grant.public_id, device=device, epoch=grant.epoch,
        expected_version=grant.version, nonce=str(body["lease_nonce"]),
        activation_jwt=activation_jwt,
    )
    return ok({
        "grant": serialize_grant(_reload(grant)),
        "activation_jwt": activation_jwt,
        "lease_jwt": lease_jwt,
    }, status=200)


# ── POST grants/{id}/lease/ ───────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def renew_lease(request, grant_id):
    """Every second, for as long as control lasts.

    🔴 No ``Idempotency-Key`` and no replay cache — see the module
    docstring. A replayed lease is a lease that outlives its own deadline.
    """
    device = device_svc.authenticate_device(
        request, path=f"grants/{grant_id}/lease/")
    body = parse_body(request, required=("epoch", "expected_version", "nonce"))

    lease_jwt, _ = lease_svc.mint_lease(
        grant_id, device=device, epoch=body["epoch"],
        expected_version=body["expected_version"],
        nonce=str(body["nonce"]),
    )
    return ok({"lease_jwt": lease_jwt})


# ── POST grants/{id}/pause/ ───────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def pause_grant(request, grant_id):
    if request.headers.get("Authorization", "").startswith("DPoP "):
        return _pause_as_device(request, grant_id)
    return _pause_as_browser(request, grant_id)


@collaboration_endpoint()
def _pause_as_browser(request, grant_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, _route("pause"), principal)
    if replay is not None:
        return replay
    body = parse_body(request, required=("expected_version",),
                      optional=("reason",))
    _load_for_browser(grant_id, principal)  # 404 before anything else
    grant = grant_svc.pause(
        grant_id, expected_version=body["expected_version"],
        reason=body.get("reason") or "", actor_user=principal.user)
    return commit(ok(serialize_grant(_reload(grant))))


@device_endpoint
def _pause_as_device(request, grant_id):
    device = device_svc.authenticate_device(
        request, path=f"grants/{grant_id}/pause/")
    body = parse_body(request, required=("expected_version",),
                      optional=("reason",))
    grant = grant_svc.pause(
        grant_id, expected_version=body["expected_version"],
        reason=body.get("reason") or "", actor_device=device)
    return ok(serialize_grant(_reload(grant)))


# ── POST grants/{id}/resume-intent/ ───────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def resume_intent(request, grant_id):
    """The target asks for control back. Stays paused."""
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, _route("resume-intent"),
                                       principal)
    if replay is not None:
        return replay
    body = parse_body(request, required=("expected_version",))
    grant = grant_svc.resume_intent(
        grant_id, principal=principal,
        expected_version=body["expected_version"])
    return commit(ok(serialize_grant(grant)))


# ── POST grants/{id}/terminate/ ───────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def terminate_grant(request, grant_id):
    if request.headers.get("Authorization", "").startswith("DPoP "):
        return _terminate_as_device(request, grant_id)
    return _terminate_as_browser(request, grant_id)


@collaboration_endpoint()
def _terminate_as_browser(request, grant_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, _route("terminate"), principal)
    if replay is not None:
        return replay
    # `expected_version` is required by the contract and deliberately NOT
    # enforced: "if version stale still revoke current resource after auth
    # (stop wins)". It is read so the shape stays conformant and so a client
    # that omits it is told, rather than silently getting a different
    # concurrency model from every other mutation.
    body = parse_body(request, required=("reason", "expected_version"))
    _load_for_browser(grant_id, principal)
    grant = grant_svc.terminate(
        grant_id, reason=body["reason"],
        expected_version=body.get("expected_version"),
        actor_user=principal.user)
    return commit(ok(serialize_grant(_reload(grant))))


@device_endpoint
def _terminate_as_device(request, grant_id):
    device = device_svc.authenticate_device(
        request, path=f"grants/{grant_id}/terminate/")
    body = parse_body(request, required=("reason", "expected_version"))
    grant = grant_svc.terminate(
        grant_id, reason=body["reason"],
        expected_version=body.get("expected_version"),
        actor_device=device)
    return ok(serialize_grant(_reload(grant)))
