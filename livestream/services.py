from datetime import timedelta
from livekit.api import AccessToken, VideoGrants
from django.conf import settings


def generate_livekit_token(user, session, is_teacher=False):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
        identity=str(user.id),
        name=user.email,
        ttl=timedelta(minutes=10),   # 👈 IMPORTANT
    )

    grants = VideoGrants(
        room_join=True,
        room=session.room_name,
        can_publish=is_teacher,
        can_subscribe=True,
    )

    token.with_grants(grants)

    return token.to_jwt()
