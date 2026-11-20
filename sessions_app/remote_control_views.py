"""The retired teacher→student screen-control path (demoted by RC-008 2c).

**This module no longer grants anything.** It is a compatibility adapter:
two endpoints that refuse, and one that can still *stop* a grant made
before the retirement. ``api-contracts.md`` and RC-008's prompt both require
it — "legacy request/respond never mint native authority" — and the reason
is worth stating plainly rather than leaving to the commit log.

What this used to do, and why it had to stop:

``respond_remote_control`` moved a grant straight from ``requested`` to
``active`` on a **browser click alone**. Shiksha Remote's entire consent
design rests on the opposite rule — a browser approval reserves a device
and nothing more; only the companion's locally-operated physical dialog can
reach ``active``. Leaving a second, weaker door into the same ``active``
state would have made that guarantee a matter of which URL a caller chose.

It was already unreachable in practice: nothing in the codebase ever sets
``GroupSessionParticipant.is_sharing_screen``, so ``request_remote_control``
always answered 409 ``not_sharing``. Demoting it turns "unreachable by
accident" into "refused on purpose", which is the difference between a
guarantee and a coincidence.

🔴 **The one real hole this closes.** ``revoke_remote_control`` looked a
grant up by raw ``id`` with no ``schema_version`` filter, so a **v1**
grant — one belonging to the new collaboration plane — could be terminated
through here. That bypasses ``collaboration.grants`` entirely: no version
bump, no audit row, no notification. Both ends would keep showing live
control over a grant the database had already revoked, and the only thing
that would eventually correct them is lease expiry. It now refuses anything
that is not a legacy row and points the caller at the proper plane.

Rows written by this module keep ``schema_version = 0``. The collaboration
state machine refuses to address them at all (``grants._lock_grant`` filters
to v1), so the two worlds cannot reach into each other in either direction.

``GlobalSettings.live_remote_access_enabled`` still gates this module and
still defaults **True** — it is not the switch for Shiksha Remote. See the
help text on it and on ``collaboration_bindings_enabled``.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import RemoteControlGrant

#: What a retired endpoint tells a caller. 410 rather than 404: the route
#: existed and has been withdrawn, which is a different thing from never
#: having been there, and a client that distinguishes them can log usefully.
RETIRED_DETAIL = (
    "Screen control has moved to Shiksha Remote, which requires a paired "
    "companion and a local approval on the device being controlled."
)
RETIRED_CODE = "legacy_remote_control_retired"


def _push(session_id, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"group_session_chat_{session_id}", payload
        )
    except Exception:
        pass


def _retired():
    return Response({"code": RETIRED_CODE, "detail": RETIRED_DETAIL},
                    status=410)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_remote_control(request, session_id):
    """Retired. Creates nothing.

    Previously this inserted a ``requested`` grant after a role and
    membership check. It is now a flat refusal — there is no argument and no
    account state that makes it create a row, which is what "never mint
    native authority" has to mean to be worth anything.
    """
    return _retired()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def respond_remote_control(request, session_id):
    """Retired. Cannot move any grant to ``active``.

    🔴 This is the endpoint the retirement is actually about. A browser
    click must never be sufficient to open an injection gate, and while this
    existed it was.
    """
    return _retired()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def revoke_remote_control(request, session_id):
    """Still works — stopping must keep working after a retirement.

    Deliberately NOT demoted alongside the other two. A grant made before
    this change may still be sitting ``active`` in some deployment's
    database, and withdrawing the only endpoint that can stop it would be
    the opposite of safe. Retiring the ways to *start* something while
    keeping the way to *stop* it is the correct asymmetry.

    🔴 Restricted to ``schema_version = 0``. A v1 grant must be terminated
    through ``collaboration.grants.terminate`` so the version moves, the
    audit row is written and both ends are told; see this module's
    docstring for what went wrong when it was not.
    """
    grant = get_object_or_404(
        RemoteControlGrant,
        id=request.data.get("grant_id"),
        session_id=session_id,
        schema_version=RemoteControlGrant.SCHEMA_LEGACY,
        status=RemoteControlGrant.STATUS_ACTIVE,
    )
    if request.user.id not in (
        grant.controller_id,
        grant.target_id,
        grant.session.host_id,
    ):
        return Response({"detail": "Not allowed."}, status=403)

    grant.status = RemoteControlGrant.STATUS_REVOKED
    grant.ended_at = timezone.now()
    grant.ended_by = "target" if request.user.id == grant.target_id else "controller"
    grant.save(update_fields=["status", "ended_at", "ended_by"])
    _push(session_id, {"type": "remote_control_revoked", "grant_id": grant.id})
    return Response({"status": grant.status})
