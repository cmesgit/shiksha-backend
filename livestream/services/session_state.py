# Redis-backed cache of a live session's status.
#
# Two things were wrong here and both were the kind that only show up on the
# day you least want them to:
#
#   * the client was hardcoded to 127.0.0.1:6379, ignoring REDIS_PLATFORM_URL
#     / REDIS_CHANNELS_URL entirely. Fine on a single box; silently broken the
#     moment Redis moves to its own host or gains a password.
#   * get_session_state() raised on any connection problem, and the live
#     consumer calls it unguarded inside connect(). A Redis hiccup therefore
#     killed the WebSocket for everyone in every class — no chat, no status
#     updates — even though the consumer has a perfectly good DB fallback
#     sitting right underneath the call.
#
# This module is a CACHE. Nothing here is the source of truth (LiveSession is),
# so every operation degrades to "no cached value" rather than raising.

import json
import logging

import redis
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_client = None


def _redis():
    """Lazily built from settings, so a bad URL fails at use, not at import.

    Module-level construction meant a misconfigured URL broke `manage.py`
    itself — including the migrate that would have fixed it.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            getattr(settings, "REDIS_PLATFORM_URL", "redis://127.0.0.1:6379/2"),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _client


def get_client():
    """Shared platform-Redis client for the live-session caches.

    Exported so consumers.py's chat cache uses the same configured endpoint
    instead of building its own hardcoded one — two modules disagreeing about
    where Redis lives is how half a feature ends up pointing at the wrong box.
    """
    return _redis()


def _key(session_id):
    return f"live_session:{session_id}"


def set_session_state(session):
    data = {
        "status": session.computed_status(),
        "teacher_left_at": (
            session.teacher_left_at.isoformat()
            if session.teacher_left_at else None
        ),
        "last_activity_at": timezone.now().isoformat(),
    }
    try:
        _redis().set(_key(session.id), json.dumps(data), ex=3600)
        return True
    except Exception:
        logger.warning("session_state: cache write failed (session=%s)",
                       session.id, exc_info=True)
        return False


def get_session_state(session_id):
    """Cached state, or None. Never raises — callers fall back to the DB."""
    try:
        data = _redis().get(_key(session_id))
    except Exception:
        logger.warning("session_state: cache read failed (session=%s)",
                       session_id, exc_info=True)
        return None
    if data:
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            # A corrupt/legacy value must not be worse than a cache miss.
            return None
    return None
