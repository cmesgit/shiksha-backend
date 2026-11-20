"""Collaboration control-plane REST routes (RC-006).

Mounted at ``/api/sessions/collaboration/v1/`` — the prefix named in
`api-contracts.md`. Everything is new; no existing route, URL or model is
touched, and no existing join flow changes. A binding is *additive
registration after the existing join*, never a second login.

Routes here (the HTTP half of RC-006):

    GET    csrf/                      safe bootstrap
    POST   bindings/                  register as an interactive participant
    POST   bindings/<id>/heartbeat/   renew, every 2s
    DELETE bindings/<id>/             release
    GET    snapshot/?binding_id=      what this binding may see
    POST   ws-ticket/                 one-use credential for the event socket

The socket those tickets open is ``collaboration/consumers.py``; the events
it carries are ``collaboration/events.py``.
"""
import logging
import uuid

from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from . import bindings as binding_store
from . import policy
from .auth import (
    collaboration_endpoint,
    error,
    idempotency_guard,
    issue_csrf_token,
    ok,
    parse_body,
)
from .policy import PolicyError

logger = logging.getLogger(__name__)


def _server_time():
    return timezone.now().isoformat()


def _require_binding(binding_id, principal):
    """Load a binding this principal still owns, or raise.

    A binding belonging to somebody else and a binding that never existed are
    the same 404 — a stranger must not be able to probe for live binding ids.
    """
    try:
        uuid.UUID(str(binding_id))
    except (TypeError, ValueError):
        raise PolicyError("not_found", "No such binding.", status=404)

    try:
        record = binding_store.get(binding_id)
    except binding_store.StoreUnavailable:
        raise PolicyError(
            "dependency_unavailable",
            "Collaboration is temporarily unavailable.",
            status=503,
        )

    if record is None or not binding_store.owned_by(record, principal):
        raise PolicyError("not_found", "No such binding.", status=404)
    return record


# ── GET csrf/ ─────────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint(unsafe=False)
def csrf_bootstrap(request):
    """`{csrf_token}` from Django's own ``get_token``, plus a refreshed cookie.

    Safe bootstrap only: it grants no API access and disables no check. The
    browser echoes the masked token in ``X-CSRFToken`` on unsafe calls.
    """
    policy.principal_from_request(request)
    return ok({"csrf_token": issue_csrf_token(request)})


# ── POST bindings/ ────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def create_binding(request):
    principal = policy.principal_from_request(request)
    body = parse_body(request, required=("session", "participant_sid"))

    session_ref = body.get("session")
    if not isinstance(session_ref, dict):
        raise PolicyError(
            "invalid_body", "session must be {kind, id}.", status=400
        )
    unknown = sorted(set(session_ref) - {"kind", "id"})
    if unknown:
        raise PolicyError(
            "unknown_fields",
            f"Unexpected field(s) in session: {', '.join(unknown)}.",
            status=400,
        )

    claimed_sid = body.get("participant_sid")
    if not isinstance(claimed_sid, str) or not claimed_sid.strip():
        raise PolicyError(
            "invalid_participant_sid", "participant_sid is required.", status=400
        )

    session = policy.resolve_session(session_ref.get("kind"), session_ref.get("id"))
    policy.assert_may_bind(principal, session)

    replay, commit = idempotency_guard(request, "bindings:create", principal)
    if replay is not None:
        return replay

    # 🔴 Identity is derived from the server's own join contract, never read
    # from the body — "server derives identity from existing token policy,
    # not body". The client only asserts the SID, and even that is checked
    # against LiveKit rather than believed.
    identity = policy.livekit_identity(principal.user, session)

    try:
        matches = binding_store.verify_participant_sid(
            session.room_name, identity, claimed_sid, allow_refresh=True
        )
    except binding_store.RosterUnavailable:
        # NOT 403. "We could not ask LiveKit" and "your SID is wrong" are
        # different answers; only one of them is worth a retry.
        return error(
            "dependency_unavailable",
            "Could not confirm your presence in the room. Try again.",
            status=503,
        )
    if not matches:
        raise PolicyError(
            "participant_sid_mismatch",
            "You are not currently connected to this room.",
            status=403,
        )

    capabilities = policy.capabilities_for(principal, session)
    try:
        binding_id, _record = binding_store.create(
            principal, session, claimed_sid, identity, capabilities
        )
    except binding_store.StoreUnavailable:
        return error(
            "dependency_unavailable",
            "Collaboration is temporarily unavailable.",
            status=503,
        )

    return commit(ok({
        "binding_id": binding_id,
        "identity": identity,
        "participant_sid": claimed_sid,
        "expires_in": binding_store.BINDING_TTL_SECONDS,
        "capabilities": capabilities,
    }, status=201))


# ── POST bindings/<id>/heartbeat/ ─────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def heartbeat_binding(request, binding_id):
    """Renew a binding. Every 2s, per participant.

    🔴 A DB **read** plus Redis, never a row write — see ``bindings`` module
    docstring. It also must not hit LiveKit per call: the roster behind
    ``verify_participant_sid`` is coalesced to one query per room per 2s.

    Explicitly carries no Idempotency-Key: the contract exempts heartbeats,
    and caching a 24h replay of a 2s-cadence call would be absurd.
    """
    principal = policy.principal_from_request(request)
    body = parse_body(request, required=("participant_sid",))
    record = _require_binding(binding_id, principal)

    claimed_sid = body.get("participant_sid")
    if claimed_sid != record.get("participant_sid"):
        # "SID change 409" — the tab reconnected to LiveKit under a new SID,
        # so this binding describes a connection that no longer exists.
        raise PolicyError(
            "participant_sid_changed",
            "Your room connection changed. Register a new binding.",
            status=409,
        )

    # Membership is re-checked on every renewal, so removal from the room,
    # the session ending, or losing entitlement all stop the binding within
    # one TTL without anything having to actively revoke it.
    try:
        session = policy.resolve_session(
            record.get("session_kind"), record.get("session_id")
        )
        policy.assert_may_bind(principal, session)
    except PolicyError:
        # "membership stale 503" per the contract row. The binding is dropped
        # so the client re-binds rather than retrying against a dead record.
        try:
            binding_store.delete(binding_id)
        except binding_store.StoreUnavailable:
            pass
        return error(
            "membership_stale",
            "You are no longer an active participant in this room.",
            status=503,
        )

    try:
        binding_store.renew(binding_id, record)
    except binding_store.StoreUnavailable:
        return error(
            "dependency_unavailable",
            "Collaboration is temporarily unavailable.",
            status=503,
        )

    return ok({
        "expires_in": binding_store.BINDING_TTL_SECONDS,
        "server_time": _server_time(),
    })


# ── DELETE bindings/<id>/ ─────────────────────────────────────────────────


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def delete_binding(request, binding_id):
    """Release a binding and terminate grants that reference it.

    Called on page leave. A lost request is covered by the 6s TTL, so this is
    an optimisation on top of expiry rather than the mechanism itself.
    """
    principal = policy.principal_from_request(request)
    record = _require_binding(binding_id, principal)

    replay, commit = idempotency_guard(request, "bindings:delete", principal)
    if replay is not None:
        return replay

    try:
        binding_store.delete(binding_id)
    except binding_store.StoreUnavailable:
        return error(
            "dependency_unavailable",
            "Collaboration is temporarily unavailable.",
            status=503,
        )

    _terminate_grants_for_binding(record)
    return commit(ok(None, status=204))


def _terminate_grants_for_binding(record):
    """"Terminal grants referencing binding", filled in by RC-008 part 2a.

    RC-006a left this a wired no-op so the cascade would have a call site
    waiting rather than needing release to remember a new one. It now ends
    every live grant that referenced the binding, in either role.

    A failure here must not fail the delete: the binding record is already
    gone from the store, so refusing the 204 would leave the caller retrying
    a delete that cannot succeed. Grants that outlive their binding are
    caught again on the next lease renewal, which re-reads fresh presence.
    """
    from . import grants

    binding_id = (record or {}).get("binding_id")
    if not binding_id:
        return None
    try:
        grants.terminate_for_binding(binding_id, reason="disconnected")
    except Exception:  # noqa: BLE001
        logger.warning("could not cascade grant termination for binding %s",
                       binding_id, exc_info=True)
    return None


# ── POST ws-ticket/ ───────────────────────────────────────────────────────


def _ws_url(request):
    """Where to open the event socket.

    Derived from the request rather than hardcoded to the production host in
    the contract's example, so dev, staging and prod each describe
    themselves. ``get_host()`` is already constrained by ``ALLOWED_HOSTS``,
    and ``COLLABORATION_WS_URL`` overrides it outright for a deployment that
    terminates websockets somewhere else.

    🔴 Dev cannot serve this URL at all: it runs gunicorn, which has no
    websocket support. The value is still correct to return — the socket half
    of this feature is simply unexercisable there, which is why RC-006 was
    split in the first place.
    """
    explicit = getattr(settings, "COLLABORATION_WS_URL", None)
    if explicit:
        return explicit
    scheme = "wss" if request.is_secure() else "ws"
    return f"{scheme}://{request.get_host()}/ws/collaboration/v1/"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint()
def ws_ticket(request):
    """Mint a single-use, 30-second ticket for ``/ws/collaboration/v1/``.

    The contract is "B with binding_id or D with host_binding_id; **not
    both**". Both and neither are each a 400 — an ambiguous request must not
    be resolved by picking one, because the two carry different authority.

    ``host_binding_id`` parses and is then refused with 422: host bindings
    are RC-007/RC-009 and no device can hold one yet. That is deliberately
    distinguishable from "unknown field" so a client built against the full
    contract gets *not yet*, not *you made that up*.

    ⚠️ The frozen contract requires an ``Idempotency-Key`` here (checked by
    `contracts/test_openapi.py`), and that has a consequence worth stating:
    replaying the same key returns the **same** ticket, which — being
    single-use with a 30s life — may already be spent. That is correct
    idempotency (the mint happened once), not a bug. A client that needs a
    fresh ticket must send a fresh key.
    """
    principal = policy.principal_from_request(request)
    body = parse_body(request, optional=("binding_id", "host_binding_id"))

    binding_id = body.get("binding_id")
    host_binding_id = body.get("host_binding_id")

    if binding_id and host_binding_id:
        raise PolicyError(
            "invalid_body",
            "Send exactly one of binding_id or host_binding_id.",
            status=400,
        )
    if not binding_id and not host_binding_id:
        raise PolicyError(
            "missing_fields",
            "One of binding_id or host_binding_id is required.",
            status=400,
        )
    if host_binding_id:
        raise PolicyError(
            "unsupported_capability",
            "Device sockets are not available yet.",
            status=422,
        )

    record = _require_binding(binding_id, principal)

    replay, commit = idempotency_guard(request, "ws-ticket:create", principal)
    if replay is not None:
        return replay

    try:
        ticket = binding_store.mint_ticket(binding_id, record)
    except binding_store.StoreUnavailable:
        return error(
            "dependency_unavailable",
            "Collaboration is temporarily unavailable.",
            status=503,
        )

    # 🔴 The ticket is returned and never logged — not here, not in the
    # consumer that spends it, not in a rejection reason.
    return commit(ok({
        "ticket": ticket,
        "expires_in": binding_store.TICKET_TTL_SECONDS,
        "ws_url": _ws_url(request),
    }))


# ── GET snapshot/ ─────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@collaboration_endpoint(unsafe=False)
def snapshot(request):
    """What this binding is allowed to see. Never strangers' private requests.

    ⚠️ ``host_bindings`` is always ``[]`` in RC-006a — not a simplification,
    but because no host binding can exist yet: the model and its routes are
    RC-007/RC-009. The key is present and correctly shaped so the client
    contract does not change when those land.
    """
    principal = policy.principal_from_request(request)
    binding_id = request.query_params.get("binding_id")
    if not binding_id:
        raise PolicyError("missing_fields", "binding_id is required.", status=400)
    record = _require_binding(binding_id, principal)

    return ok({
        "server_time": _server_time(),
        "host_bindings": [],
        "grants": _own_grants(principal, record),
        "transfers": _own_transfers(principal, record),
    })


def _own_grants(principal, record):
    """Grants this principal is actually a party to — controller or target.

    Scoped to the binding's own session as well as the user, so a binding in
    room A never reports a grant from room B.
    """
    from django.db.models import Q

    from sessions_app.models import RemoteControlGrant

    rows = (
        RemoteControlGrant.objects
        .filter(Q(controller_id=principal.user.id) | Q(target_id=principal.user.id))
        .filter(session_id=record.get("session_id"))
        .exclude(public_id__isnull=True)  # legacy schema_version=0 rows carry
                                          # no public_id and are not part of
                                          # the v1 contract
        .order_by("-requested_at")[:50]
    )
    return [
        {
            "id": str(g.public_id),
            "version": g.version,
            "state": g.status,
            "capabilities": list(g.capabilities or []),
            "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        }
        for g in rows
    ]


def _own_transfers(principal, record):
    """Transfers this principal is a party to, in this binding's room only.

    ``FileTransfer.id`` is itself the UUID primary key — there is no
    ``public_id`` on this model, unlike RemoteControlGrant.
    """
    from django.db.models import Q

    from sessions_app.models import FileTransfer

    rows = (
        FileTransfer.objects
        .filter(Q(sender_id=principal.user.id) | Q(recipient_id=principal.user.id))
        .filter(session_id=record.get("session_id"))
        .order_by("-created_at")[:50]
    )
    return [
        {
            "id": str(t.id),
            "version": t.version,
            "state": t.state,
            "mode": t.mode,
            "name": t.name,
            "mime": t.declared_mime,
            "size_bytes": t.size_bytes,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        }
        for t in rows
    ]
