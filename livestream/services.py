from datetime import timedelta
from django.conf import settings
from livekit import AccessToken
from livekit import VideoGrant


def generate_livekit_token(user, session, is_teacher=False):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
        identity=str(user.id),
        name=user.email,
    )

    grant = VideoGrant(
        room=session.room_name,
        room_join=True,
        can_publish=is_teacher,
        can_subscribe=True,
        can_publish_data=True,
    )

    token.add_grant(grant)

    token.with_ttl(timedelta(hours=6))

    return token.to_jwt()
