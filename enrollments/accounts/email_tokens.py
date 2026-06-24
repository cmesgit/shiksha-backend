import jwt
from datetime import datetime, timedelta
from django.conf import settings


def generate_email_verification_token(user):
    payload = {
        "sub": str(user.id),
        "type": "email_verification",
        "exp": datetime.utcnow() + timedelta(minutes=30),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_email_verification_token(token):
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=["HS256"],
    )
