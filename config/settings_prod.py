from .settings_base import *
import os
from django.core.exceptions import ImproperlyConfigured

# settings_base falls back to a public, well-known key when SECRET_KEY isn't
# set — fine for local/test settings (settings_dev/settings_test never hit
# this file), not fine for the real deployment: that fallback would forge
# sessions, password-reset tokens, and signed cookies. Fail loudly instead of
# booting silently onto it.
if SECRET_KEY == "django-insecure-fallback":  # noqa: F405
    raise ImproperlyConfigured(
        "SECRET_KEY is not set in the environment — refusing to start "
        "production with the insecure fallback key."
    )

# settings_base sets SECURE_SSL_REDIRECT; settings_dev explicitly turns HSTS
# off for the same reason (nginx terminates TLS, Gunicorn/Uvicorn see plain
# HTTP). Nothing previously turned it ON for the real deployment.
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

ALLOWED_HOSTS = [
    "api.shikshacom.com",
    "admin.shikshacom.com",
    "68.183.81.236",
    "localhost",
    "127.0.0.1",
]

CORS_ALLOWED_ORIGINS = [
    "https://shikshacom.com",
    "https://admin.shikshacom.com",
    "https://www.shikshacom.com",
    "https://app.shikshacom.com",
    "https://teacher.shikshacom.com",
]

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "https://shikshacom.com",
    "https://admin.shikshacom.com",
    "https://www.shikshacom.com",
    "https://app.shikshacom.com",
    "https://teacher.shikshacom.com",
    "https://api.shikshacom.com",
]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
