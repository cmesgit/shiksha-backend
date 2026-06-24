import json
from datetime import timedelta

from django.conf import settings
from livekit.api import AccessToken, VideoGrants


def generate_livekit_token(
    user,
    session,
    is_teacher=False,
    display_name=None,
    allow_publish=None,   # ✅ NEW (IMPORTANT)
):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    token.with_identity(str(user.id))

    # ✅ display name
    if display_name is None:
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            display_name = profile.full_name
        else:
            display_name = user.get_full_name() or user.username

    token.with_name(display_name)

    # ✅ metadata (role info)
    token.with_metadata(json.dumps({
        "role": "presenter" if is_teacher else "viewer",
        "user_type": "teacher" if user.has_role("TEACHER") else "student",
        "user_id": str(user.id),
    }, default=str))

    # ✅ TTL
    token.with_ttl(timedelta(hours=2))

    # ✅ room
    room_name = getattr(session, "room_name", None)
    if not room_name:
        raise ValueError("Session has no room_name")

    # =========================================================
    # 🔥 FIXED PERMISSION LOGIC (PRIVATE + PUBLIC SUPPORT)
    # =========================================================

    # If explicitly provided (like private session) → use it
    if allow_publish is not None:
        can_publish = allow_publish
    else:
        # fallback → role-based
        can_publish = True if is_teacher else False

    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=True,
    )

    token.with_grants(grants)

    return token.to_jwt()
