"""The host-binding routes (RC-009a).

`api-contracts.md` §Participant and capture APIs. The HTTP half of
``host_bindings``; every decision lives in that module, and these views
parse, authenticate the plane, delegate and serialise.

    POST   host-bindings/                B  register intent
    GET    host-bindings/{id}/           B|D read
    POST   host-bindings/{id}/share/     D  local selection -> join token
    POST   host-bindings/{id}/ready/     D  the track is live
    POST   host-bindings/{id}/heartbeat/ D  every 2s
    PATCH  host-bindings/{id}/preferences/ B|D the assistance switch
    DELETE host-bindings/{id}/           B|D stop sharing

🔴 **Who may advance a binding is the security property here.** The owner's
browser creates it and can stop it; only the *device* can share, declare
ready, or heartbeat. "No browser API can cause unattended capture" is
enforced by that split, not by a comment — a browser that could call
``share`` would be a browser that could start a screen capture.

🔴 **The join token is returned exactly once, to a device.** It is a
credential; it never appears in a browser response, in the GET body, or in
a log.
"""
import uuid

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from . import bindings as binding_store
from . import companion_tokens
from . import devices as device_svc
from . import host_bindings as host_store
from . import policy
from .auth import collaboration_endpoint, error, idempotency_guard, ok, parse_body
from .device_views import device_endpoint
from .policy import PolicyError


def _as_policy_error(exc):
    """host_bindings speaks its own error type; the boundary speaks
    PolicyError. One translation point rather than try/except everywhere."""
    return PolicyError(exc.code, exc.detail, status=exc.status)


def serialize(record, *, for_device=False):
    """The contract's host-binding shape.

    The capture descriptor is included for the two principals who are
    entitled to it — the owner and the bound device. Room-wide visibility
    goes through ``host_bindings.public_availability`` instead, which omits
    geometry entirely.
    """
    return {
        "id": record["host_binding_id"],
        "state": record["state"],
        "owner": {"user_id": record["owner_user_id"],
                  "display_name": record.get("owner_display_name") or ""},
        "device": {"id": record["device_id"],
                   "name": record.get("device_name") or ""},
        "capture": record.get("capture"),
        "allow_requests": bool(record.get("allow_requests")),
        "session": {"kind": record.get("session_kind"),
                    "id": record.get("session_id")},
        "reason": record.get("reason") or None,
        **({"identity": record.get("identity")} if for_device else {}),
    }


# ── POST host-bindings/ ───────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def create_host_binding(request):
    """The owner says "I intend to share this desktop". Captures nothing."""
    principal = policy.principal_from_request(request)
    replay, commit = idempotency_guard(request, "host-bindings:create",
                                       principal)
    if replay is not None:
        return replay

    body = parse_body(request, required=("binding_id", "device_id"))
    record = _require_own_browser_binding(body["binding_id"], principal)
    session = policy.resolve_session(record.get("session_kind"),
                                     record.get("session_id"))

    device = _device_or_404(body["device_id"])
    display_name = (principal.user.get_full_name() or "").strip() \
        or principal.user.username

    try:
        host_binding_id, created = host_store.create(
            principal=principal, session=session, device=device,
            browser_binding_id=body["binding_id"],
            owner_display_name=display_name)
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    except host_store.StoreUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Collaboration is temporarily unavailable.",
                          status=503)

    return commit(ok({
        "host_binding_id": host_binding_id,
        "state": created["state"],
        "expires_at": created["readiness_deadline"],
    }, status=201))


def _require_own_browser_binding(binding_id, principal):
    try:
        record = binding_store.get(str(binding_id))
    except binding_store.StoreUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Collaboration is temporarily unavailable.",
                          status=503)
    if record is None or not binding_store.owned_by(record, principal):
        raise PolicyError("not_found", "No such binding.", status=404)
    return record


def _device_or_404(device_id):
    from sessions_app.models import PairedDevice
    try:
        uuid.UUID(str(device_id))
    except (TypeError, ValueError):
        raise PolicyError("not_found", "No such device.", status=404)
    device = PairedDevice.objects.filter(id=device_id).first()
    if device is None:
        raise PolicyError("not_found", "No such device.", status=404)
    return device


# ── GET / DELETE host-bindings/{id}/ ──────────────────────────────────────


@api_view(["GET", "DELETE"])
@permission_classes([AllowAny])
def host_binding_detail(request, host_binding_id):
    """Both planes, dispatched on the credential actually presented."""
    is_device = request.headers.get("Authorization", "").startswith("DPoP ")
    if request.method == "GET":
        return (_read_as_device(request, host_binding_id) if is_device
                else _read_as_browser(request, host_binding_id))
    return (_close_as_device(request, host_binding_id) if is_device
            else _close_as_browser(request, host_binding_id))


@collaboration_endpoint(unsafe=False)
def _read_as_browser(request, host_binding_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    record = _load(host_binding_id)
    if not host_store.owned_by(record, principal):
        # Not the owner: only the room-safe availability view, and only once
        # the owner has opened themselves to requests.
        if not record.get("allow_requests"):
            raise PolicyError("not_found", "No such share.", status=404)
        return ok(host_store.public_availability(record))
    return ok(serialize(record))


@device_endpoint
def _read_as_device(request, host_binding_id):
    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/")
    record = _load(host_binding_id)
    if record.get("device_id") != str(device.id):
        raise PolicyError("not_found", "No such share.", status=404)
    return ok(serialize(record, for_device=True))


@collaboration_endpoint()
def _close_as_browser(request, host_binding_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    try:
        host_store.close(host_binding_id, principal=principal)
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok(None, status=204)


@device_endpoint
def _close_as_device(request, host_binding_id):
    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/")
    try:
        host_store.close(host_binding_id, device=device, reason="local_stop")
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok(None, status=204)


def _load(host_binding_id):
    try:
        record = host_store.get(str(host_binding_id))
    except host_store.StoreUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Collaboration is temporarily unavailable.",
                          status=503)
    if record is None:
        raise PolicyError("not_found", "No such share.", status=404)
    return record


# ── POST host-bindings/{id}/share/ ────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def share_display(request, host_binding_id):
    """The device reports a local display selection and gets a join token.

    🔴 Device plane only. A browser reaching this would be a browser able to
    start a capture, which is the single thing the contract says no browser
    API may do.
    """
    from django.conf import settings

    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/share/")
    body = parse_body(request, required=("display", "local_confirm"))
    if body["local_confirm"] is not True:
        raise PolicyError("local_confirm_required",
                          "The display must be chosen on the device itself.",
                          status=403)
    try:
        record = host_store.mark_joining(host_binding_id, device=device,
                                         display=body["display"])
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)

    try:
        token, identity = companion_tokens.mint_join_token(record)
    except companion_tokens.LiveKitUnconfigured:
        raise PolicyError("dependency_unavailable",
                          "Screen sharing is temporarily unavailable.",
                          status=503)
    return ok({
        "livekit_url": getattr(settings, "LIVEKIT_URL", ""),
        "token": token,
        "identity": identity,
        "join_expires_in": companion_tokens.JOIN_TOKEN_TTL_SECONDS,
    })


# ── POST host-bindings/{id}/ready/ and /heartbeat/ ────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def mark_ready(request, host_binding_id):
    """The companion reports its published track; the server verifies it."""
    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/ready/")
    body = parse_body(request, required=("participant_sid", "track_sid",
                                         "capture_generation"))
    try:
        record = host_store.mark_ready(
            host_binding_id, device=device,
            participant_sid=body["participant_sid"],
            track_sid=body["track_sid"],
            capture_generation=body["capture_generation"],
            verify_sid=_verify_sid)
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok({"state": record["state"], "capture": record["capture"]})


def _verify_sid(room_name, identity, claimed_sid):
    """Ask LiveKit whether that identity really holds that SID.

    Returns False on a mismatch and **raises 503 on an outage**: "we cannot
    check" is not "yes". ``bindings.verify_participant_sid`` already draws
    that distinction for browser bindings and this reuses it rather than
    writing a second roster path.
    """
    try:
        return bool(binding_store.verify_participant_sid(
            room_name, identity, claimed_sid, allow_refresh=True))
    except binding_store.RosterUnavailable:
        raise PolicyError("dependency_unavailable",
                          "Could not verify the room right now.", status=503)


@api_view(["POST"])
@permission_classes([AllowAny])
@device_endpoint
def heartbeat(request, host_binding_id):
    """Every 2s while sharing. No Idempotency-Key — heartbeats are exempt."""
    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/heartbeat/")
    body = parse_body(request, required=("participant_sid", "track_sid",
                                         "capture_generation"))
    try:
        host_store.heartbeat(
            host_binding_id, device=device,
            participant_sid=body["participant_sid"],
            track_sid=body["track_sid"],
            capture_generation=body["capture_generation"])
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok({"expires_in": host_store.READY_TTL_SECONDS})


# ── PATCH host-bindings/{id}/preferences/ ─────────────────────────────────


@api_view(["PATCH"])
@permission_classes([AllowAny])
def preferences(request, host_binding_id):
    is_device = request.headers.get("Authorization", "").startswith("DPoP ")
    return (_prefs_as_device(request, host_binding_id) if is_device
            else _prefs_as_browser(request, host_binding_id))


@collaboration_endpoint()
def _prefs_as_browser(request, host_binding_id):
    if not request.user or not request.user.is_authenticated:
        return error("unauthenticated", "Sign in to continue.", status=401)
    principal = policy.principal_from_request(request)
    body = parse_body(request, required=("allow_requests",))
    if not isinstance(body["allow_requests"], bool):
        raise PolicyError("invalid_body", "allow_requests must be a boolean.",
                          status=400)
    try:
        record = host_store.set_allow_requests(
            host_binding_id, allow=body["allow_requests"], principal=principal)
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok({"host_binding_id": record["host_binding_id"],
               "allow_requests": record["allow_requests"]})


@device_endpoint
def _prefs_as_device(request, host_binding_id):
    device = device_svc.authenticate_device(
        request, path=f"host-bindings/{host_binding_id}/preferences/")
    body = parse_body(request, required=("allow_requests",))
    if not isinstance(body["allow_requests"], bool):
        raise PolicyError("invalid_body", "allow_requests must be a boolean.",
                          status=400)
    try:
        record = host_store.set_allow_requests(
            host_binding_id, allow=body["allow_requests"], device=device)
    except host_store.HostBindingError as exc:
        raise _as_policy_error(exc)
    return ok({"host_binding_id": record["host_binding_id"],
               "allow_requests": record["allow_requests"]})
