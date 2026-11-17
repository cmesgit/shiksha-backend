"""The request boundary for every collaboration route (RC-006).

Four things the rest of the codebase does not do, all required by
`api-contracts.md` §Shared types and rules:

1. **Real CSRF validation.** DRF wraps every ``APIView`` in ``csrf_exempt``
   (``rest_framework/views.py``'s ``as_view``) and re-adds the check only
   inside ``SessionAuthentication``, which this project does not load — so
   today *no* DRF endpoint in this repo validates CSRF, while authenticating
   from a cookie. These routes opt back in explicitly.
2. **Exact-Origin allowlisting**, which exists nowhere in the repo today.
   Exact string equality, never a suffix test.
3. **Idempotency-Key replay** for non-heartbeat mutations.
4. **The contract's error envelope** ``{code, detail, request_id}`` and
   ``Cache-Control: no-store`` on every response.

The feature flag is enforced here too, as a **404**: while
``collaboration_bindings_enabled`` is off these routes must be
indistinguishable from routes that do not exist.
"""
import functools
import hashlib
import json
import logging
import uuid

from django.core.cache import cache
from django.middleware.csrf import CsrfViewMiddleware, get_token
from rest_framework.response import Response

from .policy import PolicyError, allowed_origins, feature_enabled

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # contract: "Retain 24h"
_IDEM_PREFIX = "collab:idem:"


class _CSRFCheck(CsrfViewMiddleware):
    """``CsrfViewMiddleware`` whose rejection is a value, not a response.

    Same shape DRF uses in ``SessionAuthentication.enforce_csrf``; reusing
    Django's own implementation means the masked-token, referer and
    trusted-origin logic stay identical to the framework's.
    """

    def _reject(self, request, reason):
        return reason


def error(code, detail, status=400, request_id=None, field_errors=None):
    payload = {
        "code": code,
        "detail": detail,
        "request_id": request_id or str(uuid.uuid4()),
    }
    if field_errors:
        payload["field_errors"] = field_errors
    response = Response(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def ok(data, status=200):
    response = Response(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


def require_exact_origin(request):
    """Reject anything whose Origin is not an exact allowlist match.

    Browsers omit ``Origin`` on same-origin GETs, so a *missing* Origin is
    tolerated on safe methods only. On an unsafe method a missing Origin is a
    refusal: the realtime contract's rule — "Do not allow missing Origin to
    bypass browser authentication" — applies to the REST plane for the same
    reason.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        raise PolicyError("origin_required", "Missing Origin.", status=403)
    if origin not in allowed_origins():
        # Deliberately does not echo the rejected origin back.
        raise PolicyError("origin_forbidden", "Origin not allowed.", status=403)


def require_csrf(request):
    check = _CSRFCheck(lambda r: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        logger.info("collaboration CSRF rejection: %s", reason)
        raise PolicyError("csrf_failed", "CSRF verification failed.", status=403)


def issue_csrf_token(request):
    """Masked token for ``X-CSRFToken``; also marks the cookie for refresh.

    ``CsrfViewMiddleware.process_response`` is what actually sets the cookie
    — it still runs for DRF views, because ``csrf_exempt`` only suppresses
    ``process_view``.
    """
    return get_token(request)


def parse_body(request, required=(), optional=()):
    """Decode a JSON body, rejecting unknown fields.

    "All mutation bodies reject unknown fields" — an ignored field is how a
    client comes to believe it set something the server never read.
    """
    data = request.data
    if not isinstance(data, dict):
        raise PolicyError("invalid_body", "Expected a JSON object.", status=400)

    allowed = set(required) | set(optional)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PolicyError(
            "unknown_fields",
            f"Unexpected field(s): {', '.join(unknown)}.",
            status=400,
        )
    missing = sorted(f for f in required if f not in data)
    if missing:
        raise PolicyError(
            "missing_fields", f"Missing field(s): {', '.join(missing)}.", status=400
        )
    return data


def _body_fingerprint(request):
    try:
        raw = json.dumps(request.data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = ""
    return hashlib.sha256(raw.encode()).hexdigest()


def idempotency_guard(request, route, principal):
    """Return a replayed Response, or a `commit(response)` callable.

    Contract: non-heartbeat mutations require a UUID ``Idempotency-Key``;
    the same actor+route+key with the same body replays the original result,
    and a *changed* body is a 409 rather than a second execution.
    """
    raw_key = request.headers.get(IDEMPOTENCY_HEADER)
    if not raw_key:
        raise PolicyError(
            "idempotency_key_required",
            f"{IDEMPOTENCY_HEADER} header is required.",
            status=400,
        )
    try:
        uuid.UUID(str(raw_key))
    except (TypeError, ValueError):
        raise PolicyError(
            "idempotency_key_invalid",
            f"{IDEMPOTENCY_HEADER} must be a UUID.",
            status=400,
        )

    cache_key = f"{_IDEM_PREFIX}{principal.user.id}:{route}:{raw_key}"
    fingerprint = _body_fingerprint(request)

    try:
        stored = cache.get(cache_key)
    except Exception:  # noqa: BLE001
        # Unlike the binding store this is fail-OPEN on read, and that is
        # safe: losing the replay cache costs at-most-once execution, not
        # authorization. Failing closed here would make a Redis blip look
        # like a permanent 409 on a key the client cannot change.
        stored = None

    if stored:
        record = json.loads(stored)
        if record.get("fingerprint") != fingerprint:
            raise PolicyError(
                "idempotency_key_reused",
                f"This {IDEMPOTENCY_HEADER} was used with a different body.",
                status=409,
            )
        replay = Response(record.get("body"), status=record.get("status", 200))
        replay["Cache-Control"] = "no-store"
        replay["Idempotent-Replay"] = "true"
        return replay, None

    def commit(response):
        if 200 <= response.status_code < 300:
            try:
                cache.set(
                    cache_key,
                    json.dumps({
                        "fingerprint": fingerprint,
                        "status": response.status_code,
                        "body": response.data,
                    }, default=str),
                    timeout=IDEMPOTENCY_TTL_SECONDS,
                )
            except Exception:  # noqa: BLE001
                logger.warning("could not record idempotency key for %s", route)
        return response

    return None, commit


def collaboration_endpoint(unsafe=True):
    """Wrap a view with the whole boundary.

    `unsafe=True` additionally demands CSRF — safe reads take the Origin
    check only, matching ``GET csrf/`` being described as a "safe bootstrap".
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            request_id = str(uuid.uuid4())
            try:
                if not feature_enabled():
                    # 404, not 403: a disabled feature is absent.
                    return error(
                        "not_found", "Not found.", status=404, request_id=request_id
                    )
                require_exact_origin(request)
                if unsafe:
                    require_csrf(request)
                return view(request, *args, **kwargs)
            except PolicyError as exc:
                return error(
                    exc.code, exc.detail, status=exc.status, request_id=request_id
                )

        return wrapper

    return decorator
