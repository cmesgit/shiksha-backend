"""
accounts/revocation.py — make "Revoke session" actually end the session.

THE PROBLEM
───────────
Access tokens are stateless and live for an hour (SIMPLE_JWT.ACCESS_TOKEN_LIFETIME).
Blacklisting the refresh token stops the session renewing, but the already-issued
access cookie keeps working until it expires — so "Revoke" would leave a stolen
device with up to 60 minutes of access. That is not what the button says it does.

THE FIX
───────
Two layers:

  1. `blacklist_sessions_refresh_tokens()` — kills renewal. Walks the user's
     OutstandingToken rows, decodes each to read its `sid`, and blacklists the
     ones belonging to the revoked session(s). simplejwt does not index by
     custom claims, so decoding is the only way; the row count per user is
     small (one per rotation still inside the 7-day refresh window).

  2. `mark_revoked()` / `is_revoked()` — kills the outstanding access token.
     A revoked `sid` is written to the shared Redis cache with a TTL equal to
     the access-token lifetime (after which no token bearing it can still be
     valid anyway, so the entry is pure garbage). CookieJWTAuthentication checks
     this on every request: one Redis GET, no database round-trip.

Cache-miss behaviour is deliberately FAIL-OPEN: if Redis is unreachable,
`is_revoked()` returns False and the request proceeds on the strength of its
signed token. The alternative — fail closed — would log every user out of the
platform the moment Redis blipped. The renewal block in layer 1 is durable in
Postgres regardless, so the worst case of a Redis outage is that a revoked
session survives until its access token expires.
"""
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_PREFIX = "revoked_sid:"


def _ttl_seconds():
    """Access-token lifetime in seconds — how long a revoked sid must stay
    remembered. Read from settings so bumping ACCESS_TOKEN_LIFETIME can't
    silently open a window here."""
    lifetime = settings.SIMPLE_JWT.get("ACCESS_TOKEN_LIFETIME")
    return int(lifetime.total_seconds()) if lifetime else 3600


def mark_revoked(sid):
    if not sid:
        return
    try:
        cache.set(f"{_PREFIX}{sid}", 1, timeout=_ttl_seconds())
    except Exception:  # pragma: no cover - cache backend down
        # Renewal is already blocked in Postgres; losing the fast path only
        # means the current access token lives out its hour.
        logger.warning("Could not cache revocation for session %s", sid, exc_info=True)


def is_revoked(sid):
    if not sid:
        return False
    try:
        return cache.get(f"{_PREFIX}{sid}") is not None
    except Exception:  # pragma: no cover - cache backend down
        logger.warning("Revocation cache unavailable; allowing request", exc_info=True)
        return False


def blacklist_sessions_refresh_tokens(user, sids):
    """Blacklist every outstanding refresh token minted for `sids`.

    `sids` is a set of session-id strings. Tokens that fail to decode (rotated
    out, signed with a retired key, malformed) are skipped rather than raised —
    an undecodable token can't be used to refresh anyway.
    """
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )
    from rest_framework_simplejwt.tokens import RefreshToken

    wanted = {str(s) for s in sids if s}
    if not wanted:
        return 0

    killed = 0
    for row in OutstandingToken.objects.filter(user=user).iterator():
        try:
            token = RefreshToken(row.token)
        except TokenError:
            continue
        if str(token.get("sid") or "") not in wanted:
            continue
        # get_or_create, not create: a token can already be blacklisted from an
        # earlier rotation, and the table has a unique constraint on token.
        _, created = BlacklistedToken.objects.get_or_create(token=row)
        if created:
            killed += 1
    return killed


def revoke_sessions(user, sessions):
    """Revoke UserSession rows end-to-end: mark the rows, block renewal, and
    kill the outstanding access tokens. Returns the number of sessions closed."""
    sids = set()
    for s in sessions:
        s.revoke()
        sids.add(str(s.id))

    if not sids:
        return 0

    blacklist_sessions_refresh_tokens(user, sids)
    for sid in sids:
        mark_revoked(sid)
    return len(sids)
