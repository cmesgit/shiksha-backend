"""
accounts/oauth_google.py  ·  Google ID-token verification

Verifies the credential produced by Google Identity Services on the frontend
and returns a normalised claim dict. This module does the cryptography and the
claim checks; it knows nothing about ShikshaCom accounts, sessions or cookies.
That lives in the view (Phase 2).

No new dependency: `google-auth` is already in requirements.txt.

WHY THE ID-TOKEN FLOW AND NOT THE REDIRECT FLOW
───────────────────────────────────────────────
The frontend posts the credential to us directly, so there is no redirect URI
to register per subdomain, and the login response keeps the exact shape
LoginView already returns. The four dashboards' routing does not change.

THE THREE CHECKS THAT MATTER
────────────────────────────
1. **Signature + `iss`** — `verify_oauth2_token` does this against Google's
   published keys. Without it a credential is just a string anyone can type.
2. **`aud` == our client id** — a token minted for a DIFFERENT application is
   cryptographically valid and completely useless to us. Skipping this means
   any site with a Google login can mint tokens that authenticate against
   ShikshaCom. `verify_oauth2_token` enforces it when we pass the audience,
   which is why it is never called without one here.
3. **`email_verified`** — Google will happily assert an email it has not
   verified. Since the first sign-in links by email, accepting an unverified
   one turns "I claim to own alice@gmail.com" into owning Alice's account.

`hd` (hosted domain) is deliberately NOT checked — ShikshaCom users are on
consumer Gmail, not a Workspace domain.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Google mints ID tokens with one of these two issuers.
_VALID_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleAuthError(Exception):
    """Verification failed. `code` is a stable string for the API response.

    The message is safe to show a user; it never contains token material.
    """

    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def google_client_id():
    """The configured OAuth client id, or "" if Google sign-in isn't set up."""
    return (getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or "").strip()


def verify_google_id_token(credential):
    """Verify a Google ID token and return its normalised claims.

    Returns ``{"sub", "email", "name", "picture"}``.
    Raises `GoogleAuthError` on every failure path — never returns partial or
    unverified data.
    """
    if not credential:
        raise GoogleAuthError("missing_credential", "No Google credential was supplied.")

    client_id = google_client_id()
    if not client_id:
        # Refuse rather than verify with audience=None, which would accept a
        # token minted for any application on earth.
        raise GoogleAuthError(
            "not_configured",
            "Google sign-in is not configured on this server.",
        )

    # Imported lazily so the module is importable (and the rest of accounts/
    # keeps working) on an install where google-auth is absent.
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as e:  # pragma: no cover - dependency is in requirements
        logger.error(f"google-auth is not installed: {e}")
        raise GoogleAuthError(
            "not_configured", "Google sign-in is not available on this server."
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,           # ← enforces `aud`. Never call without it.
        )
    except ValueError as e:
        # Bad signature, expired, wrong audience — google-auth raises ValueError
        # for all of them and the distinction is not useful to the caller.
        logger.warning(f"Google ID token rejected: {e}")
        raise GoogleAuthError("invalid_token", "Could not verify that Google sign-in.")

    if claims.get("iss") not in _VALID_ISSUERS:
        raise GoogleAuthError("invalid_issuer", "Could not verify that Google sign-in.")

    if not claims.get("email_verified"):
        raise GoogleAuthError(
            "email_unverified",
            "That Google account's email address is not verified.",
        )

    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub or not email:
        raise GoogleAuthError("incomplete_claims", "Google returned an incomplete profile.")

    return {
        "sub": sub,
        "email": email,
        "name": (claims.get("name") or "").strip(),
        "picture": claims.get("picture") or "",
    }
