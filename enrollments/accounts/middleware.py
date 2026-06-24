# ============================================================================
# PATCH 3 — accounts/middleware.py
# ============================================================================
# The chat feature needs the ACTIVE PROFILE on the websocket scope (chat
# identity is per learner profile / per teacher identity, not per account).
# The current middleware only resolves scope["user"]. Replace the body of
# JWTAuthMiddleware.__call__ and the token helper so the claims survive.
#
# Drop-in replacement for the two relevant pieces:
# ----------------------------------------------------------------------------

from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


@database_sync_to_async
def get_user_from_token(token_key):
    """Return (user, context, active_profile_id) from an access token."""
    try:
        token = AccessToken(token_key)
        user_id = token["user_id"]
        user = User.objects.get(id=user_id)
        return user, token.get("context"), token.get("active_profile")
    except (InvalidToken, TokenError, User.DoesNotExist) as e:
        logger.warning(f"JWT auth failed: {e}")
        return AnonymousUser(), None, None


class JWTAuthMiddleware(BaseMiddleware):
    """JWT auth for Channels; also exposes context + active profile on scope."""

    async def __call__(self, scope, receive, send):
        cookies = self._get_cookies(scope)
        token = cookies.get("access")

        if token:
            user, context, active_profile_id = await get_user_from_token(token)
            scope["user"] = user
            scope["context"] = context
            scope["active_profile_id"] = active_profile_id
        else:
            scope["user"] = AnonymousUser()
            scope["context"] = None
            scope["active_profile_id"] = None

        return await super().__call__(scope, receive, send)

    def _get_cookies(self, scope):
        cookies = {}
        headers = dict(scope.get("headers", []))
        cookie_header = headers.get(b"cookie", b"")
        if isinstance(cookie_header, bytes):
            cookie_header = cookie_header.decode("utf-8", errors="ignore")
        for chunk in cookie_header.split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                cookies[key.strip()] = value.strip()
        return cookies
