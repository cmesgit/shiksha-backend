"""Backend notifications — the envelope, and who is allowed to receive one.

`realtime-event-contracts.md` §Backend notification envelope fixes the shape::

    {v:1, event_id:uuid, type, resource_id:string, version:int}

with ``type`` in ``grant.changed`` / ``device.changed`` /
``host_binding.changed`` / ``transfer.changed`` / ``session.ended``.

Three properties of this module are load-bearing:

🔴 **An event is not authority.** It carries an id and a version and nothing
else — no tokens, no filenames, no capability lists, no grant bodies. The
client's only correct reaction is to refetch the authorized resource over
REST, where the real authorization check lives. "No write to frontend state
can open native gate." Anything that would make a payload *useful on its own*
is a bug, not a convenience.

🔴 **Addressing is per binding, or per account+context — never a room.** The
contract's words are "no input, tokens, filenames or grants in broad room
groups", and the reason is that a room group contains people who are not
party to the thing that changed. ``publish`` therefore refuses to send an
event that names no recipient, rather than falling back to anything wider.

🔴 **A bad event type raises rather than being delivered.** A typo'd type is
a client that silently ignores a notification forever, which presents as "the
UI just doesn't update sometimes" — the worst possible failure for a feature
whose entire job is revocation latency.

Nothing calls ``publish`` yet. That is deliberate and mirrors
``views._terminate_grants_for_binding``: RC-008 (grants) and RC-017
(transfers) own the first real call sites, and they should find a function
that is already wired and tested rather than inventing a fan-out of their
own. The consumer that delivers these is in ``consumers.py``.
"""
import re
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

ENVELOPE_VERSION = 1

# Exactly the five names in the contract. Not extensible here: a sixth event
# type is a contract change, not an implementation detail.
EVENT_TYPES = frozenset({
    "grant.changed",
    "device.changed",
    "host_binding.changed",
    "transfer.changed",
    "session.ended",
})

# Channels requires group names to be ASCII alphanumerics, hyphens,
# underscores or periods, under 100 characters. Anything else raises deep
# inside the layer at send time, which is the wrong place to find out.
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe(value):
    return _UNSAFE.sub("-", str(value or ""))[:48]


def binding_group(binding_id):
    """The one tab that holds this binding."""
    return f"collab.b.{_safe(binding_id)}"


def principal_group(user_id, context):
    """This account *in this context* — never the account across contexts.

    A learner profile and a teaching profile on the same account are
    different principals to this feature: ``bindings.owned_by`` already
    refuses to let one inherit the other's binding, and the event plane has
    to draw the line in the same place or a notification crosses it.
    """
    return f"collab.u.{_safe(user_id)}.{_safe(context) or 'none'}"


def envelope(event_type, resource_id, version):
    """Validate and build one notification. Raises ValueError, never guesses."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown collaboration event type: {event_type!r}")
    # bool is an int subclass, and `True` as a version would compare in ways
    # the client's "accept only increasing version" rule cannot survive.
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("version must be an int")
    if not resource_id:
        raise ValueError("resource_id is required")
    return {
        "v": ENVELOPE_VERSION,
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "resource_id": str(resource_id),
        "version": version,
    }


def publish(event_type, resource_id, version, *,
            binding_id=None, user_id=None, context=None):
    """Fan one notification out to the bindings/principals it names.

    Returns the envelope that was sent, so a caller can log or assert on it.
    Raises ValueError if the event names no recipient — the alternative, a
    quiet broadcast to something wider, is the failure this module exists to
    prevent.
    """
    body = envelope(event_type, resource_id, version)

    groups = []
    if binding_id:
        groups.append(binding_group(binding_id))
    if user_id:
        groups.append(principal_group(user_id, context))
    if not groups:
        raise ValueError(
            "a collaboration event must be addressed to a binding or a "
            "principal; there is no room-wide fan-out"
        )

    layer = get_channel_layer()
    if layer is None:  # no channel layer configured (e.g. a management command)
        return body
    for group in groups:
        async_to_sync(layer.group_send)(
            group, {"type": "collaboration.event", "envelope": body}
        )
    return body
