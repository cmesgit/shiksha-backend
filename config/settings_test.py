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
