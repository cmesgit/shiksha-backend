"""
LiveKit access-token issuer for Study Group rooms.

Mirrors `private_token.generate_private_token` so the frontend LiveKit
plumbing is identical, but stamps a different `type` in metadata so
clients can tell the two room kinds apart.
"""

import json
from datetime import timedelta

from django.conf import settings
from livekit.api import AccessToken, VideoGrants


def generate_study_group_token(user, session, display_name=None, role=None):
    """
    Build a short-lived LiveKit JWT for ``user`` joining ``session``.

    Parameters
    ----------
    user : accounts.User
    session : sessions_app.StudyGroupSession
    display_name : str | None
        Explicit name for the LiveKit participant. Falls back to
        ``user.profile.full_name`` / ``user.get_full_name()`` / username.
    role : str | None
        "host" / "teacher" / "student". If omitted, inferred from
        ``session.host_id`` / ``session.invited_teacher_id``.
    """
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    # Same unique-identity pattern as private sessions to avoid collisions
    # when a user has multiple rooms open in different tabs.
    token.with_identity(f"{user.id}_{session.id}")

    if display_name is None:
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            display_name = profile.full_name
        else:
            display_name = user.get_full_name() or user.username
    token.with_name(display_name)

    if role is None:
        if session.host_id == user.id:
            role = "host"
        elif session.invited_teacher_id and session.invited_teacher_id == user.id:
            role = "teacher"
        else:
            role = "student"

    token.with_metadata(json.dumps({
        "role": role,
        "type": "study_group",
        "user_id": str(user.id),
        "session_id": str(session.id),
    }))

    token.with_ttl(timedelta(minutes=60))

    room_name = session.room_name
    if not room_name:
        raise ValueError("Study group session has no room_name")

    token.with_grants(VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
    ))

    return token.to_jwt()
