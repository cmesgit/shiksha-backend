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
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
# No Redis in this sandbox — run tasks inline instead of trying (and failing) to
# reach a broker. Without this, anything that calls .delay() (e.g. seed_demo_data's
# LiveSession creation, which notifies enrollments) hangs for minutes retrying.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Local-only: lets a Vite dev server on any localhost port hit this throwaway
# server for manual browser verification. Never used outside this settings
# module (settings_dev/settings_prod are untouched).
CORS_ALLOW_ALL_ORIGINS = True
SESSION_COOKIE_SECURE = False

# The auth cookies are set with domain=settings.COOKIE_DOMAIN (auth_flow.py).
# The inherited ".shikshacom.com" makes browsers discard them outright when the
# server is localhost/127.0.0.1, so a real browser login can't be exercised
# locally. None = host-only cookie, which works on either host.
COOKIE_DOMAIN = None
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
