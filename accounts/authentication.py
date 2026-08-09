from rest_framework.exceptions import AuthenticationFailed
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

        # get_user() raises AuthenticationFailed when the token is perfectly
        # valid but its subject can't be used — "User not found" (the row is
        # gone) or "User is inactive". Because that propagated, a browser
        # holding such a cookie got 401 on EVERY endpoint, including the two it
        # needs to recover: /accounts/login/ and /accounts/logout/. AllowAny
        # never got a look in, since authentication runs first. The user could
        # not log in, could not log out, and had no way to clear an httpOnly
        # cookie from the UI — the account was unreachable until they manually
        # cleared site data.
        #
        # That is reachable in normal operation, not just in theory: SignupView
        # hard-deletes unverified accounts older than 24h, so anyone still
        # holding an access cookie for one gets locked out this way.
        #
        # Anonymous is the correct answer — same treatment this class already
        # gives a malformed token and a revoked session. Protected endpoints
        # still 401 (via IsAuthenticated), and public ones work again.
        try:
            user = self.get_user(validated_token)
        except AuthenticationFailed:
            return None

        return user, validated_token
