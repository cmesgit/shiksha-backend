import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def close_room(room_name):
    """Delete a LiveKit room server-side, disconnecting every connected
    participant immediately — without this, "End session" only flips a DB
    flag and everyone already in the call keeps publishing/consuming media
    until their token's TTL runs out (up to 2h) or LiveKit's own idle-room
    GC kicks in. Best-effort: a room that's already gone (nobody ever
    joined, or LiveKit already closed it) is not an error worth surfacing
    to the teacher who just wants their session marked ended.
    """
    if not room_name:
        return
    try:
        from asgiref.sync import async_to_sync
        from livekit import api as lk_api

        async def _delete():
            client = lk_api.LiveKitAPI(
                settings.LIVEKIT_URL,
                settings.LIVEKIT_API_KEY,
                settings.LIVEKIT_API_SECRET,
            )
            try:
                await client.room.delete_room(
                    lk_api.DeleteRoomRequest(room=room_name)
                )
            finally:
                await client.aclose()

        async_to_sync(_delete)()
    except Exception as e:
        logger.warning("LiveKit delete_room failed for %s: %s", room_name, e)


def _with_api(fn):
    """Run one LiveKit admin call, always closing the client.

    These moderation calls must NOT be best-effort in the way close_room is:
    the caller has to know whether the participant was really removed, so it
    can decide whether to tell the teacher it worked. Raises on failure.
    """
    from asgiref.sync import async_to_sync
    from livekit import api as lk_api

    async def _run():
        client = lk_api.LiveKitAPI(
            settings.LIVEKIT_URL,
            settings.LIVEKIT_API_KEY,
            settings.LIVEKIT_API_SECRET,
        )
        try:
            return await fn(client, lk_api)
        finally:
            await client.aclose()

    return async_to_sync(_run)()


def remove_participant(room_name, identity):
    """Disconnect one participant from a room.

    Note this alone is NOT enough to keep them out: a LiveKit token is a
    bearer credential with no server-side blocklist, and the livestream token
    TTL is 2 hours, so a removed student can simply rejoin with the token
    they already hold. The caller must also record the removal so the join
    endpoint refuses to mint them a new one -- see LiveSessionRemoval.
    """
    return _with_api(lambda c, api: c.room.remove_participant(
        api.RoomParticipantIdentity(room=room_name, identity=str(identity))
    ))


def mute_participant(room_name, identity, *, muted=True):
    """Force-mute (or unmute) every published track of one participant.

    LiveKit mutes a single track per call, so this fetches the participant's
    current publications and applies the change to each. A participant who is
    not in the room is not an error -- they may have just left.
    """
    async def _mute(client, api):
        info = await client.room.get_participant(
            api.RoomParticipantIdentity(room=room_name, identity=str(identity))
        )
        changed = 0
        for pub in getattr(info, "tracks", []) or []:
            await client.room.mute_published_track(
                api.MuteRoomTrackRequest(
                    room=room_name, identity=str(identity),
                    track_sid=pub.sid, muted=muted,
                )
            )
            changed += 1
        return changed

    return _with_api(_mute)
