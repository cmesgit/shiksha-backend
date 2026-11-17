"""RFC 9449 DPoP proof verification for paired devices (RC-007).

A companion has no cookie and no password. It proves possession of a P-256
private key held in OS-protected storage, on **every** request, by signing a
short-lived JWT over the method and URL it is calling. That proof is what
makes a stolen access token useless on its own.

`companion-architecture.md` fixes the profile and this module implements
exactly it, with no options:

    typ=dpop+jwt · ES256 only · public JWK in the proof header
    htm  — the HTTP method
    htu  — validated against the CONFIGURED canonical endpoint, never
           against forwarded headers
    iat  — ±30s
    jti  — unique, cached 2 minutes
    ath  — required on resource requests (hash of the access token)

🔴 **ES256 only, and the algorithm is never read from the token.** Accepting
`alg` from the header is how `none` and HMAC-confusion attacks work: a server
that trusts the header will happily verify an HS256 token using the
*public* key as the HMAC secret, and that public key is in the proof itself.
`jwt.decode(..., algorithms=["ES256"])` is passed a fixed list for that
reason. Do not make it configurable.

🔴 **`htu` comes from settings, not from the request.** `Host`,
`X-Forwarded-Host` and `X-Forwarded-Proto` are all attacker-controllable to
some degree behind a proxy, and `htu` is precisely the field that stops a
proof captured for one endpoint being replayed against another. Deriving it
from the request would make the check validate the attacker's own claim.

🔴 **The key is identified by its JWK thumbprint (RFC 7638), not by the JWK
blob.** Two byte-different JSON encodings of the same key must resolve to the
same device, and a device must not be able to re-register the same key under
a second identity by reordering fields.

🔴 **Nothing here logs a proof, a token, an `ath` or a `jti`.** Credential
material never reaches the log pipeline.

This module answers "did the holder of key K sign this exact request just
now?" and nothing else. Whether K belongs to a usable device, and what that
device may do, is `devices.py`'s question.
"""
import base64
import hashlib
import json
import time

import jwt
from django.conf import settings
from django.core.cache import cache

# The profile. None of these are configurable.
DPOP_TYP = "dpop+jwt"
DPOP_ALG = "ES256"
DPOP_IAT_LEEWAY_SECONDS = 30
DPOP_JTI_CACHE_SECONDS = 120

_JTI_PREFIX = "collab:dpop:jti:"


class DPoPError(Exception):
    """A proof that cannot be trusted.

    `detail` is deliberately coarse. Telling a caller *which* field failed
    helps them iterate towards a forgery; the server logs nothing and the
    client is told only that the proof was rejected.
    """

    def __init__(self, reason, detail="Invalid device proof."):
        super().__init__(reason)
        self.reason = reason        # for tests, never for a response body
        self.detail = detail


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 SHA-256 thumbprint, base64url, for an EC P-256 key.

    The member set and their order are fixed by the RFC — `crv`, `kty`, `x`,
    `y` for EC — and the JSON must have no whitespace. Anything else produces
    a different digest for the same key, which would let one key masquerade
    as two devices.
    """
    required = ("crv", "kty", "x", "y")
    missing = [k for k in required if not jwk.get(k)]
    if missing:
        raise DPoPError("jwk_incomplete")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise DPoPError("jwk_unsupported")
    canonical = json.dumps(
        {k: jwk[k] for k in required}, separators=(",", ":"), sort_keys=True
    )
    return b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def access_token_hash(access_token: str) -> str:
    """The `ath` value: base64url SHA-256 of the access token, per RFC 9449."""
    return b64url(hashlib.sha256(access_token.encode("ascii")).digest())


def canonical_url(path: str) -> str:
    """The `htu` this server expects for `path`.

    Built from `COLLABORATION_CANONICAL_URL` — an operator-configured origin
    — precisely so that a proxy header cannot influence it. Query and
    fragment are excluded, as RFC 9449 requires.
    """
    base = getattr(settings, "COLLABORATION_CANONICAL_URL", "") or ""
    return base.rstrip("/") + "/" + path.lstrip("/")


def _claim_jti(jti: str) -> None:
    """Burn a jti for 2 minutes, atomically. A reused one is a replay."""
    if not jti or not isinstance(jti, str) or len(jti) < 16:
        raise DPoPError("jti_invalid")
    try:
        # `add` only succeeds if the key is absent — the atomicity that makes
        # this a replay check rather than a race.
        fresh = cache.add(f"{_JTI_PREFIX}{jti}", "1", timeout=DPOP_JTI_CACHE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        # 🔴 Fails CLOSED, like bindings.py and unlike the house style. An
        # unreachable replay cache means we cannot tell a fresh proof from a
        # replayed one, and guessing "fresh" is the whole attack.
        raise DPoPError("replay_cache_unavailable",
                        "Device authentication is temporarily unavailable.") from exc
    if not fresh:
        raise DPoPError("jti_replayed")


def verify_proof(proof, *, method, path, access_token=None, expected_thumbprint=None):
    """Verify one DPoP proof and return `(thumbprint, claims)`.

    `expected_thumbprint` pins the proof to a key already on record; omit it
    during pairing, where the key is being introduced for the first time and
    is therefore self-asserted by definition.

    Raises DPoPError on anything suspect. There is no "mostly valid".
    """
    if not proof or not isinstance(proof, str):
        raise DPoPError("proof_missing")
    if len(proof) > 4096:
        # A proof is a small JWT. An oversized one is either a confused
        # client or an attempt to make us spend CPU before any check runs.
        raise DPoPError("proof_too_large")

    try:
        header = jwt.get_unverified_header(proof)
    except jwt.PyJWTError as exc:
        raise DPoPError("proof_malformed") from exc

    if header.get("typ") != DPOP_TYP:
        # Stops a token minted for another purpose being presented as a proof.
        raise DPoPError("typ_invalid")
    if header.get("alg") != DPOP_ALG:
        raise DPoPError("alg_invalid")

    jwk = header.get("jwk")
    if not isinstance(jwk, dict):
        raise DPoPError("jwk_missing")
    if jwk.get("d"):
        # A private key in a public header is a client bug serious enough to
        # refuse outright rather than tolerate.
        raise DPoPError("jwk_private")

    thumbprint = jwk_thumbprint(jwk)
    if expected_thumbprint is not None and not _constant_eq(
            thumbprint, expected_thumbprint):
        raise DPoPError("jwk_mismatch")

    try:
        key = jwt.PyJWK.from_dict({**jwk, "alg": DPOP_ALG}).key
    except Exception as exc:  # noqa: BLE001
        raise DPoPError("jwk_unusable") from exc

    try:
        claims = jwt.decode(
            proof,
            key=key,
            # 🔴 Fixed list. Never `header["alg"]`.
            algorithms=[DPOP_ALG],
            options={
                "require": ["jti", "htm", "htu", "iat"],
                "verify_aud": False,
                "verify_exp": False,   # DPoP bounds lifetime via iat, not exp
            },
        )
    except jwt.PyJWTError as exc:
        raise DPoPError("signature_invalid") from exc

    if claims.get("htm") != method.upper():
        raise DPoPError("htm_mismatch")

    expected_htu = canonical_url(path)
    if not expected_htu.startswith("http"):
        # Refusing to validate is safer than validating against "" — an
        # unconfigured canonical URL would otherwise accept every htu.
        raise DPoPError("canonical_url_unconfigured",
                        "Device authentication is not configured.")
    if claims.get("htu") != expected_htu:
        raise DPoPError("htu_mismatch")

    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        raise DPoPError("iat_invalid")
    if abs(time.time() - float(iat)) > DPOP_IAT_LEEWAY_SECONDS:
        raise DPoPError("iat_out_of_window")

    if access_token is not None:
        # `ath` binds the proof to one access token, so a proof captured
        # alongside token A cannot be reused to spend token B.
        if claims.get("ath") != access_token_hash(access_token):
            raise DPoPError("ath_mismatch")
    elif "ath" in claims:
        # A proof carrying `ath` where none is expected is being replayed
        # from a resource request onto an unauthenticated route.
        raise DPoPError("ath_unexpected")

    # Last, because burning a jti for a proof we were going to reject anyway
    # would let an attacker evict other devices' entries from the cache.
    _claim_jti(claims.get("jti"))

    return thumbprint, claims


def _constant_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(str(a), str(b))
