"""The owner's share of a display, from intent to live capture (RC-009a).

Spec: `api-contracts.md` §Participant and capture APIs. A **host binding**
is the thing RC-008 kept referring to and could not resolve: it ties one
paired device to one participant who has *locally* chosen to share one
display in one room. Until it exists, `POST grants/` has no way to derive a
target or a device from a request body without accepting them from the
caller, which the contract forbids.

Four phases, from the contract: ``awaiting_local_share`` → ``joining`` →
``ready`` → ``closed``/``failed``.

    POST   host-bindings/              B  owner registers intent
    POST   host-bindings/{id}/share/   D  native display selection -> token
    POST   host-bindings/{id}/ready/   D  the track is actually published
    POST   host-bindings/{id}/heartbeat/ D  every 2s while sharing
    DELETE host-bindings/{id}/         B|D stop sharing

🔴 **A browser can never cause capture.** The owner's browser registers an
*intent* and gets back an id; it is the companion, after a local display
selection the user performed on the desktop, that calls ``share`` and
receives a join token. "No browser API can cause unattended capture" is the
contract's wording, and the split between who creates the binding and who
can advance it is how that is enforced rather than merely stated.

🔴 **Failed and closed are terminal, and that is deliberate.** The contract
says "Failed/closed bindings cannot restart, so create a fresh binding after
local approval". A binding that could be revived would let a capture that
stopped for an unexplained reason resume without the owner touching the
device again.

🔴 **`allow_requests` defaults False.** A device that is sharing a screen
is not thereby offering it for control. The owner enables assistance
separately, and setting it back to False cancels pending requests while
leaving active control to an explicit Stop — turning the switch off must
not be a silent way to drop someone mid-session without the Stop path
running.

Storage mirrors ``bindings``: a Redis record with a TTL, never a table.
A host binding is presence, and presence that outlives its process is a
lie. The one difference is the readiness deadline — 30s to get from intent
to a published track, then a 6s renewed TTL like any other binding.
"""
import json
import logging
import time
import uuid

from django.core.cache import cache

from .bindings import BINDING_LEASE_FRESHNESS_SECONDS, BINDING_TTL_SECONDS

logger = logging.getLogger(__name__)

#: "readiness deadline 30s, renewed TTL 6s after ready"
READINESS_DEADLINE_SECONDS = 30
READY_TTL_SECONDS = BINDING_TTL_SECONDS
HEARTBEAT_SECONDS = 2

STATE_AWAITING = "awaiting_local_share"
STATE_JOINING = "joining"
STATE_READY = "ready"
STATE_CLOSED = "closed"
STATE_FAILED = "failed"

LIVE_STATES = frozenset({STATE_AWAITING, STATE_JOINING, STATE_READY})
TERMINAL_STATES = frozenset({STATE_CLOSED, STATE_FAILED})

_PREFIX = "collab:hostbinding:"
#: One live host binding per device ("max 1/device" on the create row).
_DEVICE_PREFIX = "collab:hostbinding:device:"


class StoreUnavailable(Exception):
    """The store could not be reached. Always 503, never 'no such binding' —
    the same distinction ``bindings.StoreUnavailable`` draws, for the same
    reason: an outage must not read as a clean refusal."""


class HostBindingError(Exception):
    def __init__(self, code, detail, status=409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _key(host_binding_id):
    return f"{_PREFIX}{host_binding_id}"


def _device_key(device_id):
    return f"{_DEVICE_PREFIX}{device_id}"


def _write(host_binding_id, record, ttl):
    try:
        cache.set(_key(host_binding_id), json.dumps(record), timeout=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("host binding store unavailable on write: %s", exc)
        raise StoreUnavailable() from exc


def get(host_binding_id):
    try:
        raw = cache.get(_key(host_binding_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("host binding store unavailable on read: %s", exc)
        raise StoreUnavailable() from exc
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("discarding malformed host binding %s", host_binding_id)
        delete(host_binding_id)
        return None
    if _past_readiness_deadline(record):
        # Lazy, like grant expiry: a deadline is not a state until something
        # writes it down. Reading is the moment we can.
        fail(host_binding_id, reason="readiness_timeout")
        return None
    return record


def _past_readiness_deadline(record):
    if record.get("state") not in (STATE_AWAITING, STATE_JOINING):
        return False
    return time.time() > float(record.get("readiness_deadline", 0))


def create(*, principal, session, device, browser_binding_id,
           owner_display_name=""):
    """Register the owner's intent to share. Creates no capture.

    Returns ``(host_binding_id, record)``. The device must belong to the
    caller: a host binding is a statement about *your own* desktop, and one
    naming someone else's device would be an invitation sent on their
    behalf.
    """
    if device.owner_id != principal.user.id:
        raise HostBindingError("device_not_owned",
                               "That device is not yours.", status=403)
    if not device.is_usable:
        raise HostBindingError("device_revoked",
                               "That device is not paired.", status=409)

    existing_id = _device_live_binding(device.id)
    if existing_id:
        raise HostBindingError(
            "device_busy",
            "That device is already sharing. Stop the existing share first.",
            status=409,
        )

    host_binding_id = str(uuid.uuid4())
    now = time.time()
    record = {
        "host_binding_id": host_binding_id,
        "state": STATE_AWAITING,
        "owner_user_id": str(principal.user.id),
        "owner_display_name": owner_display_name,
        "owner_context": principal.context,
        "owner_profile_id": principal.profile_id,
        "owner_auth_sid": principal.auth_sid,
        "device_id": str(device.id),
        "device_name": device.name,
        "browser_binding_id": str(browser_binding_id),
        "session_kind": "group",
        "session_id": str(session.id),
        "room_name": session.room_name,
        "capture": None,
        "participant_sid": None,
        "capture_generation": None,
        # Defaults False — sharing is not offering.
        "allow_requests": False,
        "created_at": now,
        "heartbeat_at": now,
        "readiness_deadline": now + READINESS_DEADLINE_SECONDS,
    }
    _write(host_binding_id, record, READINESS_DEADLINE_SECONDS)
    _claim_device(device.id, host_binding_id)
    return host_binding_id, record


def _claim_device(device_id, host_binding_id):
    try:
        cache.set(_device_key(device_id), host_binding_id,
                  timeout=READINESS_DEADLINE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable() from exc


def _device_live_binding(device_id):
    """The host binding currently claiming this device, if it is still live.

    The claim key is a hint, not the truth: it can outlive the record it
    points at. So it is resolved against the record rather than trusted, or
    a device whose binding lapsed would stay unusable until the claim's own
    TTL ran out.
    """
    try:
        claimed = cache.get(_device_key(device_id))
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable() from exc
    if not claimed:
        return None
    record = get(claimed)
    if record is None or record.get("state") in TERMINAL_STATES:
        _release_device(device_id)
        return None
    return claimed


def _release_device(device_id):
    try:
        cache.delete(_device_key(device_id))
    except Exception:  # noqa: BLE001
        pass


def mark_joining(host_binding_id, *, device, display):
    """The companion reports a local display selection and is given a token.

    The descriptor comes from the native side and is validated here, not
    trusted: the contract fixes every bound, and a browser must never be
    able to influence geometry ("do not accept geometry from controller
    request").
    """
    record = _require(host_binding_id, device=device)
    if record["state"] not in (STATE_AWAITING, STATE_JOINING):
        raise HostBindingError("invalid_state",
                               f"This share is {record['state']}.", status=409)

    descriptor = validate_descriptor(display)
    record["state"] = STATE_JOINING
    record["capture"] = descriptor
    record["heartbeat_at"] = time.time()
    _write(host_binding_id, record, READINESS_DEADLINE_SECONDS)
    return record


def mark_ready(host_binding_id, *, device, participant_sid, track_sid,
               capture_generation, verify_sid=None):
    """The track is actually published, verified against LiveKit.

    🔴 ``verify_sid`` is injected rather than called directly so the
    verification is testable without LiveKit credentials — the same reason
    RC-006 patches ``bindings._fetch_room_roster``. It must still be
    *supplied* in production: a companion asserting its own SID is a
    companion asserting its own presence.
    """
    record = _require(host_binding_id, device=device)
    if record["state"] not in (STATE_JOINING, STATE_READY):
        raise HostBindingError("invalid_state",
                               f"This share is {record['state']}.", status=409)
    if not participant_sid or not track_sid:
        raise HostBindingError("invalid_body",
                               "participant_sid and track_sid are required.",
                               status=400)

    from .companion_tokens import companion_identity
    identity = companion_identity(host_binding_id)

    if verify_sid is not None and not verify_sid(record["room_name"], identity,
                                                 participant_sid):
        raise HostBindingError(
            "participant_sid_mismatch",
            "That participant is not in the room under this identity.",
            status=409,
        )

    generation = _coerce_generation(capture_generation)
    capture = dict(record.get("capture") or {})
    capture["track_sid"] = track_sid
    capture["capture_generation"] = generation

    record["state"] = STATE_READY
    record["capture"] = capture
    record["participant_sid"] = participant_sid
    record["capture_generation"] = generation
    record["identity"] = identity
    record["heartbeat_at"] = time.time()
    record.pop("readiness_deadline", None)
    _write(host_binding_id, record, READY_TTL_SECONDS)
    _claim_device(record["device_id"], host_binding_id)
    return record


def heartbeat(host_binding_id, *, device, participant_sid, track_sid,
              capture_generation):
    """Every 2s while sharing. A changed descriptor or SID closes the share.

    "Resize/rotation/topology/track replacement invalidates capture_generation
    and pauses then requires new locally approved binding/grant." Closing is
    how that is enforced here — a generation change is not something to
    absorb quietly, because every grant built on this binding was authorised
    against the old one.
    """
    record = _require(host_binding_id, device=device)
    if record["state"] != STATE_READY:
        raise HostBindingError("invalid_state",
                               f"This share is {record['state']}.", status=409)

    generation = _coerce_generation(capture_generation)
    changed = (
        participant_sid != record.get("participant_sid")
        or track_sid != (record.get("capture") or {}).get("track_sid")
        or generation != record.get("capture_generation")
    )
    if changed:
        fail(host_binding_id, reason="capture_changed")
        raise HostBindingError(
            "capture_changed",
            "The shared display changed. Share again from the companion.",
            status=409,
        )

    record["heartbeat_at"] = time.time()
    _write(host_binding_id, record, READY_TTL_SECONDS)
    return record


def set_allow_requests(host_binding_id, *, allow, principal=None, device=None):
    """The owner's assistance switch. Never enables capture.

    Turning it **off** cancels pending requests but deliberately does not
    terminate an active grant: the contract says active control ends by
    explicit Stop, so that a flipped switch cannot drop someone mid-session
    without the Stop path's ordering guarantees running.
    """
    record = _require(host_binding_id, principal=principal, device=device)
    record["allow_requests"] = bool(allow)
    _write(host_binding_id, record, _ttl_for(record))

    if not allow:
        _cancel_pending_requests(host_binding_id)
    return record


def _cancel_pending_requests(host_binding_id):
    from . import grants as grant_svc
    from sessions_app.models import RemoteControlGrant

    for public_id in list(
            RemoteControlGrant.objects
            .filter(host_binding_id=host_binding_id,
                    schema_version=RemoteControlGrant.SCHEMA_V1,
                    status=grant_svc.REQUESTED)
            .values_list("public_id", flat=True)):
        try:
            grant_svc.terminate(public_id, reason="user_stop", moderator=True)
        except grant_svc.GrantError:
            continue


def close(host_binding_id, *, principal=None, device=None,
          reason="closed_by_owner"):
    """Stop sharing. Terminates every grant that referenced this binding."""
    record = _require(host_binding_id, principal=principal, device=device)
    _terminate(record, STATE_CLOSED, reason)
    return record


def fail(host_binding_id, *, reason):
    """Internal terminal transition — no actor check, used by deadlines."""
    try:
        raw = cache.get(_key(host_binding_id))
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        delete(host_binding_id)
        return None
    if record.get("state") in TERMINAL_STATES:
        return record
    _terminate(record, STATE_FAILED, reason)
    return record


def _terminate(record, state, reason):
    record["state"] = state
    record["reason"] = reason
    record["capture"] = None
    host_binding_id = record["host_binding_id"]
    # Kept briefly so a client that polls learns *why* rather than meeting a
    # bare 404, which is indistinguishable from "never existed".
    _write(host_binding_id, record, 30)
    _release_device(record.get("device_id"))

    from . import grants as grant_svc
    try:
        grant_svc.terminate_for_binding(host_binding_id, reason="capture_lost")
    except Exception:  # noqa: BLE001
        logger.warning("could not cascade grant termination for host binding %s",
                       host_binding_id, exc_info=True)


def delete(host_binding_id):
    try:
        cache.delete(_key(host_binding_id))
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable() from exc


def _ttl_for(record):
    if record.get("state") == STATE_READY:
        return READY_TTL_SECONDS
    return READINESS_DEADLINE_SECONDS


def _require(host_binding_id, *, principal=None, device=None):
    record = get(host_binding_id)
    if record is None:
        raise HostBindingError("not_found", "No such share.", status=404)
    if record.get("state") in TERMINAL_STATES:
        raise HostBindingError("invalid_state",
                               f"This share is {record['state']}.", status=409)
    if device is not None and record.get("device_id") != str(device.id):
        raise HostBindingError("not_found", "No such share.", status=404)
    if principal is not None and not owned_by(record, principal):
        raise HostBindingError("not_found", "No such share.", status=404)
    if device is None and principal is None:
        raise ValueError("_require needs an actor")
    return record


def owned_by(record, principal):
    """Same exact-actor rule as ``bindings.owned_by`` — re-logging in or
    switching profile does not inherit a share."""
    if record is None:
        return False
    return (
        record.get("owner_user_id") == str(principal.user.id)
        and record.get("owner_auth_sid") == principal.auth_sid
        and record.get("owner_context") == principal.context
        and record.get("owner_profile_id") == principal.profile_id
    )


def is_fresh_for_lease(record, *, now=None):
    """The host half of the 3s rule RC-008 could not enforce."""
    stamped = (record or {}).get("heartbeat_at")
    if stamped is None:
        return False
    age = (now or time.time()) - float(stamped)
    return age <= BINDING_LEASE_FRESHNESS_SECONDS


# ── Descriptor validation ────────────────────────────────────────────────


def _coerce_generation(value):
    try:
        generation = int(value)
    except (TypeError, ValueError):
        raise HostBindingError("invalid_capture_generation",
                               "capture_generation must be an integer.",
                               status=400)
    if generation < 0:
        raise HostBindingError("invalid_capture_generation",
                               "capture_generation must not be negative.",
                               status=400)
    return generation


#: Every bound here is from api-contracts.md §Field precision. They are not
#: defensive guesses — an out-of-range value is a malformed descriptor, and
#: accepting one would put geometry the injection adapter trusts outside the
#: range it was designed against.
MAX_DIMENSION = 32768
MAX_PIXEL_AREA = 268435456
ROTATIONS = frozenset({0, 90, 180, 270})
MIN_SCALE, MAX_SCALE = 0.25, 8.0
MAX_DISPLAY_ID = 128
INT32 = 2 ** 31


def validate_descriptor(display):
    """Return a normalised capture descriptor, or raise.

    ``track_sid`` is explicitly **not** accepted here: the share step
    describes a display, and the track does not exist until the companion
    has actually published it. The contract's share row says
    "descriptor_without_track_sid" for exactly that reason.
    """
    if not isinstance(display, dict):
        raise HostBindingError("invalid_display", "display must be an object.",
                               status=400)
    if "track_sid" in display:
        raise HostBindingError(
            "invalid_display",
            "track_sid is reported at ready, not at share.", status=400)

    display_id = display.get("display_id")
    if not isinstance(display_id, str) or not display_id or \
            len(display_id) > MAX_DISPLAY_ID:
        raise HostBindingError("invalid_display",
                               "display_id must be a string of 1..128 chars.",
                               status=400)

    width = _positive_int(display.get("pixel_width"), "pixel_width")
    height = _positive_int(display.get("pixel_height"), "pixel_height")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise HostBindingError("invalid_display",
                               "display dimensions are out of range.",
                               status=400)
    if width * height > MAX_PIXEL_AREA:
        raise HostBindingError("invalid_display",
                               "display pixel area is out of range.",
                               status=400)

    rotation = display.get("rotation", 0)
    if rotation not in ROTATIONS:
        raise HostBindingError("invalid_display",
                               "rotation must be 0, 90, 180 or 270.",
                               status=400)

    scale = display.get("dpi_scale", 1.0)
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        raise HostBindingError("invalid_display", "dpi_scale must be a number.",
                               status=400)
    # NaN fails both comparisons, which is the intent: "nonfinite numbers
    # are rejected".
    if not (MIN_SCALE <= scale <= MAX_SCALE):
        raise HostBindingError("invalid_display",
                               "dpi_scale must be between 0.25 and 8.",
                               status=400)

    bounds = display.get("desktop_bounds") or {}
    if not isinstance(bounds, dict):
        raise HostBindingError("invalid_display",
                               "desktop_bounds must be an object.", status=400)
    x = _int32(bounds.get("x", 0), "desktop_bounds.x")
    y = _int32(bounds.get("y", 0), "desktop_bounds.y")
    bw = _positive_int(bounds.get("width", width), "desktop_bounds.width")
    bh = _positive_int(bounds.get("height", height), "desktop_bounds.height")

    source = display.get("source", "display")
    if source != "display":
        # "Browser tab/window capture remains view-only" — only a whole
        # display can back a control grant.
        raise HostBindingError("unsupported_capture_source",
                               "Only a full display can be controlled.",
                               status=422)

    return {
        "display_id": display_id,
        "pixel_width": width,
        "pixel_height": height,
        "rotation": int(rotation),
        "dpi_scale": scale,
        "desktop_bounds": {"x": x, "y": y, "width": bw, "height": bh},
        "coordinate_space": "physical_pixels",
        "source": "display",
        "track_sid": None,
        "capture_generation": None,
    }


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HostBindingError("invalid_display",
                               f"{field} must be a positive integer.",
                               status=400)
    return value


def _int32(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or \
            not (-INT32 <= value < INT32):
        raise HostBindingError("invalid_display",
                               f"{field} must be a 32-bit signed integer.",
                               status=400)
    return value


def public_availability(record):
    """What a would-be controller may see before they are allowed to ask.

    "Snapshot host availability exposes only host_binding_id, owner
    name/UUID, device label, ready flag and capabilities until controller is
    allowed to request; descriptor is fetched only for that ready host."
    The descriptor is deliberately absent — display geometry is a fingerprint
    of someone's desk, not a room-wide fact.
    """
    return {
        "host_binding_id": record["host_binding_id"],
        "owner": {"user_id": record["owner_user_id"],
                  "display_name": record.get("owner_display_name") or ""},
        "device": {"name": record.get("device_name") or ""},
        "ready": record.get("state") == STATE_READY,
        "allow_requests": bool(record.get("allow_requests")),
    }
