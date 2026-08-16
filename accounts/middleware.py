# PLACEMENT: backend/backend/accounts/middleware.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/accounts/middleware.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The old middleware read the JWT ONLY from the `access` cookie. Three frontend
# hooks (useNotificationSocket, GroupSessionClassroomUI, PrivateSessions notify)
# pass the token as `?token=<jwt>` instead — which was silently ignored, so
# those sockets connected as AnonymousUser and were closed.
#
# Resolution order is now:
#   1. `access` cookie            (production path — unchanged, still preferred)
#   2. `?token=` query parameter  (localhost / cross-port dev, native clients)
#
# Everything else (claims → scope["context"] / scope["active_profile_id"]) is
# unchanged, so the chat identity resolution keeps working exactly as before.

from urllib.parse import parse_qs

from django.conf import settings
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
import logging

from .revocation import is_revoked

logger = logging.getLogger(__name__)
User = get_user_model()


@database_sync_to_async
def get_user_from_token(token_key):
    """Return (user, context, active_profile_id, identity) from an access
    token. `identity` is the M1 claim (Phase 3 §7) — absent on any token
    minted before this deploy, which is expected and fine: every consumer
    of scope["identity"] treats a missing claim as "fall back to the
    context + active_profile_id resolution," never as an error.

    Mirrors CookieJWTAuthentication's two extra checks (accounts/
    authentication.py) that this WS path was missing: a revoked session's
    access token stays valid here for up to its full ~1h lifetime instead
    of dying immediately like the REST equivalent, and a deactivated
    account (is_active=False) could otherwise keep a live chat/livestream
    socket forever."""
    try:
        token = AccessToken(token_key)
        if is_revoked(token.get("sid")):
            return AnonymousUser(), None, None, None
        user_id = token["user_id"]
        user = User.objects.get(id=user_id, is_active=True)
        return user, token.get("context"), token.get("active_profile"), token.get("identity")
    except (InvalidToken, TokenError, User.DoesNotExist) as e:
        logger.warning(f"JWT auth failed: {e}")
        return AnonymousUser(), None, None, None


class JWTAuthMiddleware(BaseMiddleware):
    """JWT auth for Channels; also exposes context + active profile on scope.

    Token sources, in order: `access` cookie, then `?token=` query param.
    """

    async def __call__(self, scope, receive, send):
        token = self._get_token(scope)

        if token:
            user, context, active_profile_id, identity = await get_user_from_token(token)
            scope["user"] = user
            scope["context"] = context
            scope["active_profile_id"] = active_profile_id
            scope["identity"] = identity
        else:
            scope["user"] = AnonymousUser()
            scope["context"] = None
            scope["active_profile_id"] = None
            scope["identity"] = None

        return await super().__call__(scope, receive, send)

    # ── token extraction ────────────────────────────────────────────────

    def _get_token(self, scope):
        # 1) Cookie (primary — matches CookieJWTAuthentication on REST).
        token = self._get_cookies(scope).get("access")
        if token:
            return token

        # 2) ?token= query param — LOCAL DEV ONLY (settings.DEBUG). A token in
        # the URL ends up in server/proxy access logs, so this must never be
        # honored on a deployed environment (dev droplet or prod both run with
        # DEBUG=False). No frontend code currently populates this in a way
        # that reaches deployed hosts (nothing writes the access token into
        # local/session storage), so restricting this is a no-op for existing
        # traffic and only closes a future log-leakage footgun.
        if not settings.DEBUG:
            return None
        try:
            qs = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            params = parse_qs(qs)
            values = params.get("token") or []
            if values and values[0]:
                return values[0]
        except Exception:
            # Malformed query strings must never crash the handshake.
            pass
        return None

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
