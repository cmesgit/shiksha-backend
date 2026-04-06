import json
from datetime import timedelta

from django.conf import settings
from livekit.api import AccessToken, VideoGrants


def generate_private_token(user, session, display_name=None):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    # 🔥 FIX 1: unique identity per session
    identity = f"{user.id}_{session.id}"
    token.with_identity(identity)

    # display name
    if display_name is None:
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            display_name = profile.full_name
        else:
            display_name = user.get_full_name() or user.username

    token.with_name(display_name)

    # 🔥 FIX 2: proper role metadata
    is_teacher = (session.teacher_id == user.id)

    token.with_metadata(json.dumps({
        "role": "teacher" if is_teacher else "student",
        "type": "private_session",
        "user_id": str(user.id),
        "session_id": str(session.id),
    }))

    # 🔥 FIX 3: safer TTL
    token.with_ttl(timedelta(minutes=60))

    room_name = session.room_name
    if not room_name:
        raise ValueError("Session has no room_name")

    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
    )

    token.with_grants(grants)

    return token.to_jwt()
