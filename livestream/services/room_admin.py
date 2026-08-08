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
