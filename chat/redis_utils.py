# PLACEMENT: backend/backend/chat/redis_utils.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/redis_utils.py
"""
chat/redis_utils.py — M0 hardening (Phase 3 architecture §13, §16, §32).

Two independent concerns, both backed by settings.REDIS_PLATFORM_URL (its own
Redis logical db — never the channel layer's db, never Celery's broker db):

  1. UNREAD COUNTERS
     Redis hash `unread:<identity_key>` maps conversation_id -> count. Written
     with HINCRBY on every new message, cleared with HDEL on mark-read. This
     is a CACHE, not the source of truth — Postgres (messages + last_read_at)
     is. get_unread_count() takes a `rebuild_fn` closure that recomputes the
     true count from the DB and backfills Redis, so a Redis flush is a cache
     miss, never data loss.

  2. RATE LIMITING
     Fixed-window counters (INCR + conditional EXPIRE) rather than a true
     token bucket / leaky bucket. This is a deliberate simplification: a
     precise token bucket needs a Lua script (or redis-py's experimental
     token_bucket) to be atomic, and for "10 msgs/10s, 200/hour" abuse
     limiting the extra precision buys nothing a fixed window doesn't already
     give you. INCR-then-check-TTL works on any Redis version (no reliance on
     Redis 7's EXPIRE NX/GT/LT flags).

EVERY function here fails OPEN and never raises: a Redis outage must degrade
chat gracefully (counters fall back to a DB count; rate limiting stops
limiting) rather than break sending or reading messages. This mirrors the
"never raises" contract notifications.services.notify() already uses.
"""
import logging

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_client = None


def get_redis():
    """Module-level singleton client (redis-py pools connections internally,
    so one client is the right lifetime — not one per call)."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_PLATFORM_URL, decode_responses=True)
    return _client


# ---------------------------------------------------------------------------
# Unread counters
# ---------------------------------------------------------------------------

def _unread_hash_key(identity_key):
    return f"unread:{identity_key}"


def increment_unread(identity_key, conversation_id):
    """Call once per (recipient identity, new message)."""
    try:
        get_redis().hincrby(_unread_hash_key(identity_key), str(conversation_id), 1)
    except Exception:
        logger.exception(
            "redis_utils: increment_unread failed identity=%s conv=%s — "
            "next read falls back to a DB count",
            identity_key, conversation_id,
        )


def clear_unread(identity_key, conversation_id):
    """Call on mark-read (REST MarkReadView and the WS 'read' event)."""
    try:
        get_redis().hdel(_unread_hash_key(identity_key), str(conversation_id))
    except Exception:
        logger.exception(
            "redis_utils: clear_unread failed identity=%s conv=%s",
            identity_key, conversation_id,
        )


def get_unread_count(identity_key, conversation_id, rebuild_fn):
    """Redis-first unread count with a DB-truth fallback.

    `rebuild_fn` is a zero-arg callable that returns the correct count from
    Postgres (the caller already has the conversation + participant in
    scope, so it builds the closure). On a cache miss we backfill Redis so
    the NEXT read for this identity+conversation is O(1) again.
    """
    key = _unread_hash_key(identity_key)
    field = str(conversation_id)
    try:
        val = get_redis().hget(key, field)
        if val is not None:
            return int(val)
    except Exception:
        logger.exception(
            "redis_utils: get_unread_count read failed identity=%s conv=%s — "
            "rebuilding from DB",
            identity_key, conversation_id,
        )

    count = rebuild_fn()

    try:
        get_redis().hset(key, field, count)
    except Exception:
        # Backfill failing is fine — we still return the correct DB count.
        logger.exception(
            "redis_utils: get_unread_count backfill failed identity=%s conv=%s",
            identity_key, conversation_id,
        )
    return count


# ---------------------------------------------------------------------------
# Rate limiting  (fixed-window; see module docstring for why not a bucket)
# ---------------------------------------------------------------------------

RATE_BURST_LIMIT = 10       # messages
RATE_BURST_WINDOW = 10      # seconds
RATE_HOURLY_LIMIT = 200     # messages
RATE_HOURLY_WINDOW = 3600   # seconds

CONNECT_LIMIT = 20          # connection attempts
CONNECT_WINDOW = 60         # seconds


def _fixed_window_hit(key, window_seconds):
    """INCR the counter; EXPIRE it only the first time (TTL == -1 means "no
    expiry set yet", which is true both for a brand-new key and — belt and
    braces — a key that somehow lost its TTL). Returns the post-increment
    count, or None if Redis itself is unavailable (caller should fail open)."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    count, ttl = pipe.execute()
    if ttl == -1:
        r.expire(key, window_seconds)
    return count


def check_message_rate_limit(identity_key):
    """Returns None if the send is allowed, or a short user-facing reason if
    the identity has exceeded the burst or hourly cap. Fails OPEN: if Redis
    itself errors, we allow the send rather than block chat on an outage."""
    try:
        burst_count = _fixed_window_hit(f"rl:burst:{identity_key}", RATE_BURST_WINDOW)
        if burst_count > RATE_BURST_LIMIT:
            return "You're sending messages too fast. Wait a few seconds and try again."

        hourly_count = _fixed_window_hit(f"rl:hourly:{identity_key}", RATE_HOURLY_WINDOW)
        if hourly_count > RATE_HOURLY_LIMIT:
            return "You've reached the hourly message limit. Try again later."

        return None
    except Exception:
        logger.exception("redis_utils: rate limit check failed for %s — failing open", identity_key)
        return None


def check_connect_rate_limit(subject_key):
    """Same fixed-window mechanism, applied to WS connection attempts.
    `subject_key` is typically the account/user id (connections happen
    before we know which identity is being selected)."""
    try:
        count = _fixed_window_hit(f"rl:connect:{subject_key}", CONNECT_WINDOW)
        return count <= CONNECT_LIMIT
    except Exception:
        logger.exception("redis_utils: connect rate limit check failed for %s — failing open", subject_key)
        return True
