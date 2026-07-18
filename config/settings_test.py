"""Throwaway test/dev settings: dev config on a local sqlite DB.

Used for running makemigrations / check / tests without Docker or postgres.
Not for production. See the backend-run-env memory.
"""
from .settings_dev import *  # noqa

import os

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("TEST_DB_PATH", "/tmp/claude-1000/shiksha_test.sqlite3"),
    }
}

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# Avoid channels/redis layer + celery brokers during isolated tests.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Run Celery tasks inline instead of dispatching to a Redis broker. Without
# this, every code path that fires a task (e.g. the session-bell / WS push in
# sessions_app._broadcast_session_update) blocks ~20s per call retrying a
# broker that isn't there. Eager mode executes the task in-process and never
# touches the broker or result backend. EAGER_PROPAGATES stays False so a
# failing task degrades quietly, mirroring the fire-and-forget production path.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False

# chat.redis_utils backs unread counters + rate limiting with a real Redis in
# production; in tests we swap in an in-process fakeredis so the suite needs no
# running redis-server. See chat/redis_utils.get_redis().
REDIS_USE_FAKE = True
