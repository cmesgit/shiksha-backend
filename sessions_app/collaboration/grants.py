"""The consent state machine for control grants (RC-008 part 2a).

Spec: ``docs/architecture/shiksha-remote/remote-control-protocol.md`` §State
model is the normative transition table; ``api-contracts.md`` §Control grants
fixes the routes that call into here. This module owns **every** write to a
v1 ``RemoteControlGrant``. Nothing else may set ``status``.

**RC-008 is split, and this is part 2a.** Part 1 built the PostgreSQL 16
concurrency harness. Part 2b owns the Ed25519 activation ticket, the bounded
1-second leases, demoting ``remote_control_views.py`` and wiring the routes.
The split is the same one part 1 made and for the same reason: the whole of
part 2 is past this workspace's size rule, and the state machine is the half
whose central claim — *simultaneous grants, one winner* — is the thing the
harness exists to prove. Crypto on top of an unproven machine would be the
wrong order.

Four properties are load-bearing, and all four are easy to lose in a later
refactor:

🔴 **The device row is locked before the grant row, always.** Two orders
deadlock. ``_lock_grant`` peeks at ``device_id`` without a lock, takes the
device, and only then takes the grant — so every path through this module
acquires the two in the same order as ``request_control``'s insert.

🔴 **Expiry is applied lazily, under the lock, before every transition.**
Nothing may infer "this is expired" from a clock comparison in an index or a
serializer. ``database-specification.md`` is explicit that the partial unique
index carries no ``NOW()``: a deadline that has passed is not a state until
some transaction has written it down. A late cleanup job must change nothing.

🔴 **Audit is written in the same transaction as the transition; the
notification is published only after commit.** A rolled-back transition that
had already told a browser "you have control" is the worst failure this
feature can produce.

🔴 **``expected_version`` is mandatory on every actor-driven transition,
except a stop.** The contract says of terminate: *"if version stale still
revoke current resource after auth (stop wins)"*. Stop beats consistency;
nothing else does.

A note on a name that means the opposite of what it looks like:
``group_session_policy``'s ``remote_control_enabled`` is **legacy** — it is
``True`` for scheduled rooms and ``False`` for instant ones, which is the
reverse of OD-01's rule for this plane (peer control inside instant
collaborations, teacher-initiated only in scheduled ones). This module must
never read it. ``assert_may_request_control`` restates OD-01 directly.
"""
import functools
import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from global_settings.models import GlobalSettings

from . import devices as device_svc
from . import events
from .policy import ALL_CAPABILITIES, PolicyError

logger = logging.getLogger(__name__)

# ── Deadlines, all from remote-control-protocol.md §State model ───────────
REQUEST_TTL = timedelta(seconds=60)        # "expiry = request + 60s"
APPROVAL_TTL = timedelta(seconds=30)       # "approved deadline ≤30s"
GRANT_MAX = timedelta(minutes=10)          # "min(now+10min, room hard end)"
PAUSE_HOLD = timedelta(seconds=60)         # "hold reservation ≤60s"
RESUME_INTENT_TTL = timedelta(seconds=30)  # "intent expires 30s"

CONSENT_NONCE_BYTES = 32

# ── The nine states ───────────────────────────────────────────────────────
REQUESTED = "requested"
APPROVED = "approved"
ACTIVE = "active"
PAUSED = "paused"
DECLINED = "declined"
REVOKED = "revoked"
EXPIRED = "expired"
COMPLETED = "completed"
FAILED = "failed"

NONTERMINAL = frozenset({REQUESTED, APPROVED, ACTIVE, PAUSED})
TERMINAL = frozenset({DECLINED, REVOKED, EXPIRED, COMPLETED, FAILED})
# The three that occupy the one-live-grant-per-device slot. Must stay
# byte-identical to the partial unique index in RemoteControlGrant.Meta.
DEVICE_RESERVING = frozenset({APPROVED, ACTIVE, PAUSED})

#: Legal transitions, as ``to_state -> {from_state}``. Terminal rows never
#: reactivate, so no terminal state appears on the right-hand side.
TRANSITIONS = {
    DECLINED: frozenset({REQUESTED}),
    APPROVED: frozenset({REQUESTED}),
    ACTIVE: frozenset({APPROVED, PAUSED}),
    PAUSED: frozenset({ACTIVE}),
    REVOKED: NONTERMINAL,
    EXPIRED: NONTERMINAL,
    COMPLETED: frozenset({ACTIVE, PAUSED}),
    FAILED: frozenset({APPROVED, ACTIVE, PAUSED}),
}

#: Reasons a terminate may carry, from api-contracts.md §Control grants.
TERMINATE_REASONS = frozenset({
    "user_stop", "completed", "local_stop", "disconnected",
    "capture_lost", "protocol_error", "session_ended",
})
#: Which terminal state each reason lands in. ``completed`` is the only
#: ordinary finish; everything else is a failure or an explicit revocation.
_REASON_TO_STATE = {
    "completed": COMPLETED,
    "user_stop": REVOKED,
    "local_stop": REVOKED,
    "session_ended": REVOKED,
    "disconnected": FAILED,
    "capture_lost": FAILED,
    "protocol_error": FAILED,
}


class GrantError(PolicyError):
    """A refusal that already knows its contract code and HTTP status."""


# ── Feature gates ─────────────────────────────────────────────────────────


def control_enabled():
    """The narrower of the two kill flags.

    ``collaboration_bindings_enabled`` turns the whole plane off, routes and
    notification socket included. ``collaboration_control_enabled`` turns off
    *control* while leaving bindings, the socket and (later) file transfer
    working — which is what an operator actually wants mid-incident: stop
    every grant without blanking the UI for everyone in a live room.

    Both must be on. The protocol's "Global flag-off invalidates new leases"
    is enforced here and re-read on every lease renewal in part 2b, never
    cached into a ticket.
    """
    conf = GlobalSettings.load()
    return bool(
        conf.collaboration_bindings_enabled and conf.collaboration_control_enabled
    )


def assert_control_enabled():
    if not control_enabled():
        # 404, matching the rest of the plane: a disabled feature is absent.
        raise GrantError("not_found", "Not found.", status=404)


# ── Capabilities ──────────────────────────────────────────────────────────


def normalise_capabilities(raw, *, allow_empty=False):
    """Sorted, unique, allowlisted, and internally consistent.

    ``api-contracts.md`` §Field precision: *"pointer.button and pointer.scroll
    each require pointer.move in the approved set; reject inconsistent
    subsets 422"*. A set that cannot be obeyed is rejected at the boundary
    rather than quietly repaired, because repairing it would hand the target
    a capability list that is not the one they were shown.
    """
    if not isinstance(raw, list):
        raise GrantError("invalid_capabilities", "capabilities must be a list.",
                         status=400)
    for name in raw:
        if not isinstance(name, str):
            raise GrantError("invalid_capabilities",
                             "capability names must be strings.", status=400)
        if name not in ALL_CAPABILITIES:
            raise GrantError("unsupported_capability",
                             f"Unknown capability {name!r}.", status=422)
    caps = sorted(set(raw))
    if not caps and not allow_empty:
        raise GrantError("invalid_capabilities",
                         "At least one capability is required.", status=422)
    needs_pointer = {"pointer.button", "pointer.scroll"} & set(caps)
    if needs_pointer and "pointer.move" not in caps:
        raise GrantError(
            "unsupported_capability",
            "pointer.button and pointer.scroll each require pointer.move.",
            status=422,
        )
    return caps


def assert_subset(approved, requested):
    """An approval may only ever narrow. Never widen, never substitute."""
    extra = sorted(set(approved) - set(requested))
    if extra:
        raise GrantError(
            "capability_superset",
            f"Cannot approve capabilities that were not requested: "
            f"{', '.join(extra)}.",
            status=422,
        )


# ── OD-01 eligibility ─────────────────────────────────────────────────────


def assert_may_request_control(principal, session, target_user):
    """May *this* principal ask *this* target for control, in *this* room?

    OD-01, resolved by the architecture owner on 11 November 2026 and
    recorded in ``architecture-decisions.md``:

    * **instant collaboration** — anyone the host admitted, independent of
      role label, including an un-authenticated ``InstantCollaborationGuest``
      whose only identity is a self-chosen display name. Lobby admission *is*
      the authorization gate.
    * **scheduled GroupSession** — teacher-initiated only. A peer may not
      request even though both are accepted invitees.

    Binding eligibility is checked separately and symmetrically by
    ``policy.assert_may_bind``; this is the asymmetric rule on top of it.
    """
    from sessions_app.group_session_policy import policy_for

    if principal.user.id == getattr(target_user, "id", None):
        # Also enforced by rc_grant_controller_is_not_target. Same-account
        # profiles are distinct contexts and the identity is ambiguous, so
        # MVP refuses rather than guessing which profile is being driven.
        raise GrantError("self_control_denied",
                         "You cannot control your own account.", status=403)

    policy = policy_for(session)
    if policy.kind == "instant":
        return
    if policy.kind == "scheduled":
        invite = session.invites.filter(user=principal.user).first()
        if policy.role(session, principal.user, invite) == "student":
            raise GrantError(
                "teacher_initiated_only",
                "In a scheduled session only the teacher may request control.",
                status=403,
            )
        return
    raise GrantError("unsupported_session_kind",
                     "Control is not available in this session type yet.",
                     status=422)


# ── Locking and expiry ────────────────────────────────────────────────────


def _lock_grant(public_id):
    """Lock the device row, then the grant row. Never the other order.

    Returns the locked grant. Raises 404 for anything that is not a v1 row:
    a legacy grant is not addressable through this plane at all, which is
    what stops the demoted legacy views (part 2b) from reaching the state
    machine by passing an id.
    """
    from sessions_app.models import PairedDevice, RemoteControlGrant

    try:
        uuid.UUID(str(public_id))
    except (TypeError, ValueError):
        raise GrantError("not_found", "No such grant.", status=404)

    peek = (
        RemoteControlGrant.objects
        .filter(public_id=public_id, schema_version=RemoteControlGrant.SCHEMA_V1)
        .values("device_id").first()
    )
    if peek is None:
        raise GrantError("not_found", "No such grant.", status=404)

    if peek["device_id"] is not None:
        PairedDevice.objects.select_for_update().filter(
            pk=peek["device_id"]).first()

    grant = (
        RemoteControlGrant.objects.select_for_update()
        .filter(public_id=public_id,
                schema_version=RemoteControlGrant.SCHEMA_V1)
        .first()
    )
    if grant is None:
        raise GrantError("not_found", "No such grant.", status=404)
    return grant


def _deadline_for(grant):
    """The deadline that governs this grant's current state, or None."""
    if grant.status == REQUESTED:
        return grant.request_expires_at
    if grant.status == APPROVED:
        return grant.approval_expires_at
    if grant.status == PAUSED:
        # A paused grant dies at whichever comes first: the end of the
        # 60-second reservation hold, or the original hard expiry. Resuming
        # must never extend the hard expiry (api-contracts.md).
        candidates = [d for d in (grant.paused_until, grant.expires_at) if d]
        return min(candidates) if candidates else None
    if grant.status == ACTIVE:
        return grant.expires_at
    return None


def expire_if_due(grant, *, now=None):
    """Write down an elapsed deadline, under the caller's existing lock.

    Returns True if the row was transitioned. Callers hold the device and
    grant locks already — this is deliberately not re-entrant and does not
    open its own transaction, because a separate transaction here would
    release the device slot outside the caller's atomic block.
    """
    now = now or timezone.now()
    if grant.status not in NONTERMINAL:
        return False
    deadline = _deadline_for(grant)
    if deadline is None or deadline > now:
        return False
    _apply(grant, EXPIRED, reason="deadline", event_type="grant.expired",
           now=now)
    return True


# ── The one write path ────────────────────────────────────────────────────


def _apply(grant, to_state, *, reason="", event_type, actor_user=None,
           actor_device=None, metadata=None, now=None, fields=None):
    """Perform one legal transition: bump version, persist, audit, notify.

    Every state change in this module goes through here. The ``fields``
    mapping carries the transition's "required atomic effects" from the
    protocol table, applied in the same ``save`` as the status change so no
    reader can observe a half-transitioned row.
    """
    from sessions_app.models import CollaborationAuditEvent

    now = now or timezone.now()
    allowed = TRANSITIONS.get(to_state)
    if allowed is None:
        raise ValueError(f"unknown target state {to_state!r}")
    if grant.status not in allowed:
        raise GrantError(
            "invalid_state",
            f"A {grant.status} grant cannot become {to_state}.",
            status=409,
        )

    grant.status = to_state
    grant.version = grant.version + 1
    if reason:
        grant.reason = reason

    changed = ["status", "version", "reason"]
    for name, value in (fields or {}).items():
        setattr(grant, name, value)
        changed.append(name)

    if to_state in TERMINAL:
        grant.ended_at = now
        grant.ended_by = (
            "device" if actor_device is not None
            else ("user" if actor_user is not None else "system")
        )
        # Release every deadline so a late cleanup job has nothing to act on,
        # and clear the one-use consent challenge.
        grant.consent_nonce_hash = None
        grant.paused_until = None
        grant.resume_intent_expires_at = None
        changed += ["ended_at", "ended_by", "consent_nonce_hash",
                    "paused_until", "resume_intent_expires_at"]

    grant.save(update_fields=sorted(set(changed)))

    ref = grant.session_ref() or {}
    CollaborationAuditEvent.objects.create(
        actor_user=actor_user,
        actor_device=actor_device,
        actor_context=(grant.controller_context if actor_user is not None
                       and actor_user.id == grant.controller_id
                       else grant.target_context),
        event_type=event_type,
        resource_type="grant",
        resource_id=str(grant.public_id),
        resource_version=grant.version,
        session_kind=ref.get("kind", ""),
        session_id=ref.get("id") or None,
        reason=reason,
        metadata=metadata or {},
    )

    _notify_after_commit(grant)
    return grant


def _notify_after_commit(grant):
    """Tell both ends to refetch — only once the transition is durable.

    The envelope carries an id and a version and nothing else; the client's
    only correct reaction is to refetch over REST. See ``events`` for why
    that is not a convenience but the whole design.
    """
    public_id = str(grant.public_id)
    version = grant.version
    recipients = [
        {"binding_id": grant.controller_binding_id},
        {"binding_id": grant.host_binding_id},
        {"user_id": grant.controller_id,
         "context": (grant.controller_context or {}).get("context")},
        {"user_id": grant.target_id,
         "context": (grant.target_context or {}).get("context")},
    ]

    def _publish():
        for kwargs in recipients:
            if not any(kwargs.values()):
                continue
            try:
                events.publish("grant.changed", public_id, version, **kwargs)
            except Exception:  # noqa: BLE001
                # A notification is a latency optimisation, never authority.
                # Losing one must not unwind a committed transition.
                logger.warning("could not publish grant.changed for %s",
                               public_id, exc_info=True)

    transaction.on_commit(_publish)


@transaction.atomic
def _sweep_grant(public_id):
    """Apply one grant's elapsed deadline, in a transaction of its own."""
    try:
        grant = _lock_grant(public_id)
    except GrantError:
        return  # nothing addressable; the caller will raise its own 404
    expire_if_due(grant)


@transaction.atomic
def _sweep_device(device):
    """Apply every elapsed deadline on one device, in its own transaction."""
    from sessions_app.models import PairedDevice, RemoteControlGrant

    PairedDevice.objects.select_for_update().filter(pk=device.pk).first()
    now = timezone.now()
    for stale in (RemoteControlGrant.objects.select_for_update()
                  .filter(device=device,
                          schema_version=RemoteControlGrant.SCHEMA_V1,
                          status__in=NONTERMINAL)):
        expire_if_due(stale, now=now)


def _sweeps_expiry(fn):
    """Run the expiry sweep *before* the decorated transaction opens.

    🔴 **This is not a tidiness wrapper; it fixes a real defect.** The
    obvious implementation calls ``expire_if_due`` inside the same atomic
    block as the work. But most callers that find an elapsed grant then
    *refuse* — 410 expired, 409 busy — and a refusal rolls the transaction
    back, taking the expiry write with it. The row stays ``requested``
    forever as far as any reader is concerned, and because
    ``rc_grant_one_live_grant_per_device`` is a partial index with no
    ``NOW()`` in it, that stale row keeps holding the device slot until some
    *successful* writer happens past. A device could be permanently
    un-requestable while every caller correctly reports it as expired.

    Found by ``test_an_elapsed_request_cannot_be_approved``, which asserted
    the post-condition rather than just the exception — the expiry raised the
    right 410 and still left the wrong row behind.

    🔴 Consequence for callers: these functions must **not** be invoked
    inside an outer ``atomic`` block, or the sweep rejoins that transaction
    and the defect returns. ``terminate_for_binding`` and
    ``terminate_for_device`` are written accordingly.
    """

    @functools.wraps(fn)
    def wrapper(public_id, *args, **kwargs):
        _sweep_grant(public_id)
        return fn(public_id, *args, **kwargs)

    return wrapper


def _assert_live(grant):
    """Report *why* a terminal grant cannot be acted on, before anything else.

    Ordering matters and is not cosmetic. Once ``_sweeps_expiry`` has written
    an elapsed deadline down, the row's version has moved — so a caller
    holding the version it last read would otherwise be told "409 version
    conflict", which invites a refetch-and-retry loop against a grant that
    can never be acted on again. "410 expired" is the terminal, actionable
    answer, and the contract reserves that status for exactly this.
    """
    if grant.status == EXPIRED:
        raise GrantError("expired", "This grant has expired.", status=410)
    if grant.status in TERMINAL:
        raise GrantError("invalid_state",
                         f"This grant is already {grant.status}.", status=409)


def _check_version(grant, expected_version):
    if expected_version is None:
        raise GrantError("expected_version_required",
                         "expected_version is required.", status=400)
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        raise GrantError("expected_version_required",
                         "expected_version must be an integer.", status=400)
    if expected != grant.version:
        raise GrantError(
            "version_conflict",
            "This grant changed since you last read it.",
            status=409,
        )


# ── Public API ────────────────────────────────────────────────────────────


def request_control(*, principal, session, device, target_user,
                    controller_binding_id, host_binding_id, capabilities,
                    room_name="", controller_identity="", controller_sid="",
                    host_identity="", host_sid=""):
    """``none -> requested``. Binds immutable principals; expiry = now + 60s.

    Requesting deliberately does **not** reserve the device: the protocol
    reserves on *approval*, so a controller cannot deny everyone else access
    to a device simply by asking. What a request does hold is the
    one-pending-request-per-(controller, device) slot, which is what stops
    the same controller flooding one device.
    """
    from sessions_app.models import PairedDevice, RemoteControlGrant

    assert_control_enabled()
    assert_may_request_control(principal, session, target_user)
    caps = normalise_capabilities(capabilities)

    if device.owner_id != getattr(target_user, "id", None):
        # The device must belong to the person being asked. Otherwise a
        # controller could name any device and push a consent prompt at its
        # owner from a room they are not in.
        raise GrantError("device_not_owned_by_target",
                         "That device does not belong to this participant.",
                         status=403)
    if not device.is_usable:
        raise GrantError("device_revoked", "That device is not paired.",
                         status=409)

    # Sweep elapsed deadlines in their own committed transaction first — see
    # _sweeps_expiry for why this cannot live inside the block below.
    _sweep_device(device)

    with transaction.atomic():
        return _request_control_locked(
            principal=principal, session=session, device=device,
            target_user=target_user, caps=caps,
            controller_binding_id=controller_binding_id,
            host_binding_id=host_binding_id, room_name=room_name,
            controller_identity=controller_identity,
            controller_sid=controller_sid, host_identity=host_identity,
            host_sid=host_sid,
        )


def _request_control_locked(*, principal, session, device, target_user, caps,
                            controller_binding_id, host_binding_id, room_name,
                            controller_identity, controller_sid,
                            host_identity, host_sid):
    from sessions_app.models import PairedDevice, RemoteControlGrant

    now = timezone.now()
    # Same lock order as _lock_grant: device first, always.
    PairedDevice.objects.select_for_update().filter(pk=device.pk).first()

    if RemoteControlGrant.objects.filter(
            device=device, schema_version=RemoteControlGrant.SCHEMA_V1,
            status__in=DEVICE_RESERVING).exists():
        raise GrantError("busy", "Another control session is active on that "
                                 "device.", status=409)
    if RemoteControlGrant.objects.filter(
            device=device, controller=principal.user,
            schema_version=RemoteControlGrant.SCHEMA_V1,
            status=REQUESTED).exists():
        raise GrantError("request_pending",
                         "You already have a request pending on that device.",
                         status=409)

    grant = RemoteControlGrant.objects.create(
        schema_version=RemoteControlGrant.SCHEMA_V1,
        public_id=uuid.uuid4(),
        session=session,
        controller=principal.user,
        target=target_user,
        device=device,
        status=REQUESTED,
        controller_context={"context": principal.context,
                            "profile_id": principal.profile_id,
                            "auth_session_id": principal.auth_sid},
        target_context=None,
        controller_binding_id=controller_binding_id,
        host_binding_id=host_binding_id,
        room_name=room_name or getattr(session, "room_name", "") or "",
        controller_identity=controller_identity,
        controller_sid=controller_sid,
        host_identity=host_identity,
        host_sid=host_sid,
        capabilities=caps,
        version=1,
        request_expires_at=now + REQUEST_TTL,
    )

    from sessions_app.models import CollaborationAuditEvent
    ref = grant.session_ref() or {}
    CollaborationAuditEvent.objects.create(
        actor_user=principal.user,
        actor_context=grant.controller_context,
        event_type="grant.requested",
        resource_type="grant",
        resource_id=str(grant.public_id),
        resource_version=grant.version,
        session_kind=ref.get("kind", ""),
        session_id=ref.get("id") or None,
        metadata={"capabilities": caps},
    )
    _notify_after_commit(grant)
    return grant


@_sweeps_expiry
@transaction.atomic
def decide(public_id, *, principal, allow, capabilities, expected_version):
    """``requested -> approved|declined``, decided by the target's browser.

    🔴 **A browser decision never mints native authority.** Approving locks
    the device and reserves the one-live-grant slot, and that is all: the
    companion still has to show a physical consent dialog and call
    ``activate``. The protocol says it twice — *"approved is not a permission
    to inject"* and *"no ACTIVE response"* — because a UI-only approval path
    is the single most tempting shortcut in this design.
    """
    assert_control_enabled()
    grant = _lock_grant(public_id)
    now = timezone.now()

    if grant.target_id != principal.user.id:
        # 404, not 403: a stranger learns nothing about whether this exists.
        raise GrantError("not_found", "No such grant.", status=404)
    expire_if_due(grant, now=now)
    _assert_live(grant)
    _check_version(grant, expected_version)
    if grant.status != REQUESTED:
        raise GrantError("invalid_state",
                         f"This request is already {grant.status}.", status=409)

    target_context = {"context": principal.context,
                      "profile_id": principal.profile_id,
                      "auth_session_id": principal.auth_sid}

    if not allow:
        caps = normalise_capabilities(capabilities or [], allow_empty=True)
        return _apply(grant, DECLINED, reason="declined",
                      event_type="grant.declined", actor_user=principal.user,
                      now=now, metadata={"capabilities": caps},
                      fields={"target_context": target_context})

    caps = normalise_capabilities(capabilities)
    assert_subset(caps, grant.capabilities or [])

    # Re-check the slot inside the lock. The partial unique index is the real
    # guarantee — this check exists to turn a would-be IntegrityError into
    # the contract's 409, not to replace the constraint.
    from sessions_app.models import RemoteControlGrant
    if (RemoteControlGrant.objects
            .filter(device_id=grant.device_id,
                    schema_version=RemoteControlGrant.SCHEMA_V1,
                    status__in=DEVICE_RESERVING)
            .exclude(pk=grant.pk).exists()):
        raise GrantError("busy", "Another control session is active on that "
                                 "device.", status=409)

    return _apply(
        grant, APPROVED, reason="", event_type="grant.approved",
        actor_user=principal.user, now=now,
        metadata={"capabilities": caps},
        fields={
            "capabilities": caps,
            "target_context": target_context,
            "approval_expires_at": now + APPROVAL_TTL,
            "request_expires_at": None,
        },
    )


@_sweeps_expiry
@transaction.atomic
def issue_consent_nonce(public_id, *, device):
    """One-use challenge the companion echoes back when it activates.

    Only the plaintext leaves here; only its keyed hash is stored, using the
    same ``devices.keyed_hash`` the pairing secrets use. A second call
    invalidates the first — the nonce is single-use by construction, so a
    companion that fetches twice cannot activate with the older value.
    """
    assert_control_enabled()
    grant = _lock_grant(public_id)
    now = timezone.now()

    if grant.device_id != device.id:
        raise GrantError("not_found", "No such grant.", status=404)
    expire_if_due(grant, now=now)
    _assert_live(grant)
    if grant.status not in (APPROVED, PAUSED):
        raise GrantError("invalid_state",
                         "No consent is pending on this grant.", status=409)
    if grant.status == PAUSED and (
            grant.resume_intent_expires_at is None
            or grant.resume_intent_expires_at <= now):
        # A paused grant needs the target's fresh resume intent first; the
        # companion cannot re-open a paused grant on its own initiative.
        raise GrantError("resume_intent_required",
                         "The owner has not asked to resume yet.", status=409)

    nonce = device_svc.new_device_secret()
    grant.consent_nonce_hash = device_svc.keyed_hash(nonce)
    grant.save(update_fields=["consent_nonce_hash"])
    return nonce


@_sweeps_expiry
@transaction.atomic
def activate(public_id, *, device, consent_nonce, expected_version,
             capture_generation, local_confirm):
    """``approved|paused -> active``, on the companion's physical consent.

    This is the only transition that can open the gate, and it is reachable
    only with device authentication plus the one-use consent nonce. The
    signed activation ticket and the first lease are minted by part 2b from
    the row this returns — deliberately not here, so the state machine stays
    provable without a key in the test environment.
    """
    assert_control_enabled()
    grant = _lock_grant(public_id)
    now = timezone.now()

    if grant.device_id != device.id:
        raise GrantError("not_found", "No such grant.", status=404)
    if local_confirm is not True:
        raise GrantError("local_confirm_required",
                         "The owner must approve on the device itself.",
                         status=403)
    expire_if_due(grant, now=now)
    _assert_live(grant)
    _check_version(grant, expected_version)

    if grant.status not in (APPROVED, PAUSED):
        raise GrantError("invalid_state",
                         f"A {grant.status} grant cannot be activated.",
                         status=409)
    if grant.status == PAUSED and (
            grant.resume_intent_expires_at is None
            or grant.resume_intent_expires_at <= now):
        raise GrantError("resume_intent_required",
                         "The owner has not asked to resume yet.", status=409)
    if not device.is_usable:
        raise GrantError("device_revoked", "This device is no longer paired.",
                         status=409)

    if grant.consent_nonce_hash is None or not device_svc.hashes_match(
            str(consent_nonce or ""), grant.consent_nonce_hash):
        raise GrantError("consent_nonce_invalid",
                         "That consent challenge is not valid.", status=403)

    try:
        generation = int(capture_generation)
    except (TypeError, ValueError):
        raise GrantError("invalid_capture_generation",
                         "capture_generation must be an integer.", status=400)
    if generation < 0:
        raise GrantError("invalid_capture_generation",
                         "capture_generation must not be negative.", status=400)

    resuming = grant.status == PAUSED
    # "retain original hard expiry" on resume; a fresh activation gets
    # min(now + 10 min, room hard end).
    expires_at = grant.expires_at if resuming else _hard_expiry(grant, now)

    return _apply(
        grant, ACTIVE, reason="", event_type="grant.activated",
        actor_device=device, now=now,
        metadata={"resumed": resuming, "capture_generation": generation},
        fields={
            # A fresh epoch on every activation, resume included: input
            # sequences reset with it, so no packet from a previous epoch can
            # be replayed into this one.
            "epoch": uuid.uuid4(),
            "expires_at": expires_at,
            "capture_generation": generation,
            "granted_at": grant.granted_at or now,
            "consent_nonce_hash": None,
            "approval_expires_at": None,
            "paused_until": None,
            "resume_intent_expires_at": None,
        },
    )


def _hard_expiry(grant, now):
    """``min(now + 10 min, room hard end)``.

    The room's own deadline wins when it is sooner: a control grant must not
    outlive the meeting it belongs to. ``RoomTiming.deadline`` is the
    existing arithmetic, including any extension already granted.
    """
    ceiling = now + GRANT_MAX
    session = grant.session
    if session is None:
        return ceiling
    from sessions_app.group_session_policy import policy_for
    room_end = policy_for(session).deadline(session)
    if room_end and room_end < ceiling:
        return room_end
    return ceiling


@_sweeps_expiry
@transaction.atomic
def pause(public_id, *, expected_version, reason="", actor_user=None,
          actor_device=None):
    """``active -> paused``. Holds the device reservation for ≤60s.

    Repeating a pause on an already-paused grant is idempotent per the
    contract, and deliberately does not extend the hold: a controller that
    keeps sending pause cannot keep a device reserved indefinitely.
    """
    assert_control_enabled()
    grant = _lock_grant(public_id)
    now = timezone.now()

    _assert_party(grant, actor_user=actor_user, actor_device=actor_device)
    expire_if_due(grant, now=now)
    _assert_live(grant)
    if grant.status == PAUSED:
        return grant
    _check_version(grant, expected_version)
    if grant.status != ACTIVE:
        raise GrantError("invalid_state",
                         f"A {grant.status} grant cannot be paused.",
                         status=409)

    return _apply(
        grant, PAUSED, reason=str(reason or "")[:64],
        event_type="grant.paused", actor_user=actor_user,
        actor_device=actor_device, now=now,
        fields={
            "epoch": uuid.uuid4(),  # bump: old packets cannot resume into this
            "paused_until": now + PAUSE_HOLD,
            "consent_nonce_hash": None,
            "resume_intent_expires_at": None,
        },
    )


@_sweeps_expiry
@transaction.atomic
def resume_intent(public_id, *, principal, expected_version):
    """The target's "I want this back", recorded on a still-paused grant.

    Stays ``paused``: there is no browser-only resume. It increments the
    version and opens a 30-second window in which the companion may fetch a
    consent nonce and re-activate behind a fresh physical approval.
    """
    assert_control_enabled()
    grant = _lock_grant(public_id)
    now = timezone.now()

    if grant.target_id != principal.user.id:
        raise GrantError("not_found", "No such grant.", status=404)
    expire_if_due(grant, now=now)
    _assert_live(grant)
    _check_version(grant, expected_version)
    if grant.status != PAUSED:
        raise GrantError("invalid_state",
                         "Only a paused grant can be resumed.", status=409)

    from sessions_app.models import CollaborationAuditEvent

    grant.version += 1
    grant.resume_intent_expires_at = now + RESUME_INTENT_TTL
    grant.consent_nonce_hash = None
    grant.save(update_fields=["version", "resume_intent_expires_at",
                              "consent_nonce_hash"])

    ref = grant.session_ref() or {}
    CollaborationAuditEvent.objects.create(
        actor_user=principal.user,
        actor_context=grant.target_context,
        event_type="grant.resume_intent",
        resource_type="grant",
        resource_id=str(grant.public_id),
        resource_version=grant.version,
        session_kind=ref.get("kind", ""),
        session_id=ref.get("id") or None,
    )
    _notify_after_commit(grant)
    return grant


@_sweeps_expiry
@transaction.atomic
def terminate(public_id, *, reason, expected_version=None, actor_user=None,
              actor_device=None, moderator=False):
    """Any nonterminal state -> ``revoked`` / ``completed`` / ``failed``.

    🔴 **Stop wins.** A stale ``expected_version`` is *not* a refusal here —
    the contract says so explicitly: *"if version stale still revoke current
    resource after auth (stop wins)"*. Authorization is still checked; only
    the optimistic-concurrency check is waived, because the alternative is a
    revoke that fails precisely when something else is changing the grant.

    Terminating an already-terminal grant is idempotent and returns the row
    unchanged, so a companion retrying a stop over a flaky link cannot turn
    a ``completed`` into a ``revoked``.
    """
    if reason not in TERMINATE_REASONS:
        raise GrantError("invalid_reason", "Unknown termination reason.",
                         status=400)
    # NB: no assert_control_enabled() here. Stopping must keep working after
    # the kill flag goes off, or flipping the flag would strand live grants.
    grant = _lock_grant(public_id)
    now = timezone.now()

    if not moderator:
        _assert_party(grant, actor_user=actor_user, actor_device=actor_device)

    if grant.status in TERMINAL:
        return grant
    if expire_if_due(grant, now=now):
        return grant

    return _apply(
        grant, _REASON_TO_STATE[reason], reason=reason,
        event_type="grant.terminated", actor_user=actor_user,
        actor_device=actor_device, now=now,
        metadata={"moderator": bool(moderator)},
    )


def _assert_party(grant, *, actor_user=None, actor_device=None):
    """Only the controller, the target or the bound device may act."""
    if actor_device is not None and grant.device_id == actor_device.id:
        return
    if actor_user is not None and actor_user.id in (grant.controller_id,
                                                    grant.target_id):
        return
    raise GrantError("not_found", "No such grant.", status=404)


# ── Cascades from elsewhere in the plane ──────────────────────────────────


def terminate_for_binding(binding_id, *, reason="disconnected"):
    """Every live grant that references a binding that just went away.

    This is the body of ``views._terminate_grants_for_binding``, which
    RC-006a left as a wired no-op precisely so this function had a call site
    waiting for it rather than needing release to remember a new cascade.
    """
    from sessions_app.models import RemoteControlGrant

    if not binding_id:
        return 0
    rows = list(
        RemoteControlGrant.objects
        .filter(schema_version=RemoteControlGrant.SCHEMA_V1,
                status__in=NONTERMINAL)
        .filter(**{"controller_binding_id": binding_id})
        .values_list("public_id", flat=True)
    ) + list(
        RemoteControlGrant.objects
        .filter(schema_version=RemoteControlGrant.SCHEMA_V1,
                status__in=NONTERMINAL)
        .filter(**{"host_binding_id": binding_id})
        .values_list("public_id", flat=True)
    )
    stopped = 0
    for public_id in dict.fromkeys(rows):
        try:
            # Deliberately NOT wrapped in an outer atomic: terminate() opens
            # its own, and its expiry sweep has to be able to commit on its
            # own. See _sweeps_expiry.
            terminate(public_id, reason=reason, moderator=True)
            stopped += 1
        except GrantError:
            # Raced to terminal by something else; nothing left to stop.
            continue
    return stopped


def terminate_for_device(device, *, reason="local_stop"):
    """Unlinking or revoking a device must stop whatever it was hosting."""
    from sessions_app.models import RemoteControlGrant

    stopped = 0
    for public_id in list(
            RemoteControlGrant.objects
            .filter(device=device,
                    schema_version=RemoteControlGrant.SCHEMA_V1,
                    status__in=NONTERMINAL)
            .values_list("public_id", flat=True)):
        try:
            terminate(public_id, reason=reason, moderator=True)
            stopped += 1
        except GrantError:
            continue
    return stopped
