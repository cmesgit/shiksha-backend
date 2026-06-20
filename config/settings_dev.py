from .settings_base import *

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}

# ── Security overrides for dev ────────────────────────────────────────────
# settings_base enables these for prod (Nginx on the droplet handles HTTPS).
# Keeping them True in dev causes SecurityMiddleware to redirect every
# Gunicorn-bound HTTP request to HTTPS, stripping CORS headers en route.
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
# ─────────────────────────────────────────────────────────────────────────

ALLOWED_HOSTS = [
    "134.209.154.122",
    "api.dev.shikshacom.com",
    "dev.api.shikshacom.com",
    "localhost",
    "127.0.0.1",
]

CSRF_TRUSTED_ORIGINS = [
    "https://api.dev.shikshacom.com",
    "https://dev.shikshacom.com",
    "https://app.dev.shikshacom.com",
    "https://teacher.dev.shikshacom.com",
    "https://admin.dev.shikshacom.com",
    "https://dev.api.shikshacom.com",
]

CORS_ALLOWED_ORIGINS = [
    "https://dev.shikshacom.com",
    "https://app.dev.shikshacom.com",
    "https://teacher.dev.shikshacom.com",
    "https://admin.dev.shikshacom.com",
    "https://dev.api.shikshacom.com",
]

CORS_ALLOW_CREDENTIALS = True

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
