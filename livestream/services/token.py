import json
from datetime import timedelta

from django.conf import settings
from livekit.api import AccessToken, VideoGrants


def build_identity(user_id, session_id):
    """The LiveKit participant identity for a live class.

    Composite, matching what group and private sessions already do. This was
    a bare str(user.id), and livestream was the only feature that did so —
    the other two carry an explicit comment about avoiding collisions when a
    user has more than one room open.

    LiveKit treats a repeated identity within a room as a replacement, so the
    bare form meant a student opening the class in a second tab silently
    killed the first with no explanation. Worse for attendance: if the kicked
    tab's participant_left landed after the new tab's participant_joined, the
    leave closed the freshly opened interval, leaving the student watching
    with no open interval at all — undercounted in the live viewer number and
    credited nothing for the rest of the lesson.
    """
    return f"{user_id}_{session_id}"


def parse_identity(raw):
    """user id out of a participant identity, tolerating the legacy form.

    Tokens are bearer credentials with a 2h TTL, so for two hours after this
    ships there are still live participants whose identity is a bare user id.
    Handling both is what stops a deploy mid-class from corrupting attendance
    for everyone already connected.
    """
    raw = str(raw or "")
    return raw.split("_", 1)[0] if "_" in raw else raw


def generate_livekit_token(
    user,
    session,
    is_teacher=False,
    display_name=None,
):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    token.with_identity(build_identity(user.id, session.id))

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

    if is_teacher:
        # 🎤 PRESENTER (creator only)
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_publish_data=True,
            can_subscribe=True,
        )
    else:
        # 👀 VIEWER (students + other teachers)
        # can_publish=True so mic works, but frontend starts muted by default
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,         # ✅ mic allowed (starts muted via frontend)
            can_publish_data=True,    # ✅ allows raise hand + chat
            can_subscribe=True,
            can_publish_sources=["microphone"],  # 🎤 mic only, no camera/screen
        )

    token.with_grants(grants)

    return token.to_jwt()