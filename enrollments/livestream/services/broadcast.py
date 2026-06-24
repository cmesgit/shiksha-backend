from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

# 🔥 NEW
from livestream.services.session_state import set_session_state


def broadcast_session_update(session_id, data, session_obj=None):
    channel_layer = get_channel_layer()

    # 🔥 UPDATE REDIS FIRST (safe)
    if session_obj:
        try:
            set_session_state(session_obj)
        except Exception:
            pass  # don't break system if Redis fails

    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"session_{session_id}",
        {
            "type": "session_update",
            "data": data,
        },
    )
