from django.conf import settings
from livekit import AccessToken, VideoGrant


def generate_livekit_token(user, session, is_teacher=False):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    token.identity = str(user.id)
    token.name = user.email

    grant = VideoGrant(
        room=session.room_name,
        room_join=True,
        can_publish=is_teacher,
        can_subscribe=True,
    )

    token.add_grant(grant)

    return token.to_jwt()
