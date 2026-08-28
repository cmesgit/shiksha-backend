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

# Throttling OFF by default in tests.
#
# Throttle state lives in the cache, which is NOT reset between test methods,
# so a real rate limit makes the suite order-dependent: any test that logs in
# a few times exhausts the budget and every later test in that class fails
# with 429 while passing in isolation. That is exactly what happened when the
# login throttle was first attached.
#
# Tests that exercise throttling re-enable it explicitly with
# @override_settings(REST_FRAMEWORK={...}) — see
# accounts.tests_lookup.LoginThrottleTest.
# Keys must remain PRESENT with a value of None — DRF looks the scope up in
# THROTTLE_RATES and raises KeyError if it is missing, so an empty dict breaks
# every throttled view instead of disabling it.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {
        scope: None
        for scope in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]  # noqa: F405
    },
}
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

# No nginx in front of local runserver/test runs — secure_media_view must
# stream files itself instead of issuing an X-Accel-Redirect nginx would
# otherwise never resolve, which would make every private-media response
# come back as literally nothing.
MEDIA_SERVED_BY_NGINX = False

# Automatic class recording (LiveKit Egress → Bunny Storage) is off here.
# settings_base already computes LIVEKIT_EGRESS_ENABLED as False whenever the
# LIVEKIT_* / BUNNY_EGRESS_* env vars are absent, which they are in this
# sandbox — this line is belt-and-braces so a developer who exports real
# LiveKit credentials to run the sessions_app tests doesn't also start
# billing egress minutes against a throwaway sqlite DB.
LIVEKIT_EGRESS_ENABLED = False
