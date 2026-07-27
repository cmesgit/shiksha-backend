from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from .revocation import is_revoked


class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        raw_token = request.COOKIES.get("access")

        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
        except InvalidToken:
            #  IMPORTANT FIX:
            # Do NOT raise here.
            # Just return None so request continues.
            return None

        # Sessions & devices: "Revoke" has to end the session *now*, not
        # whenever this stateless access token happens to expire (up to an hour
        # later). Revoked session ids are held in Redis for exactly the access
        # token's lifetime, so this is one cache GET and no database query.
        # Treated the same as a bad token — anonymous, not an exception — to
        # match the behaviour above. Tokens issued before session tracking
        # existed carry no `sid` and are unaffected.
        if is_revoked(validated_token.get("sid")):
            return None

        return self.get_user(validated_token), validated_token
