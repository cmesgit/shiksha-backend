from .settings_base import *

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}

ALLOWED_HOSTS = [
    "134.209.154.122",
    "api.dev.shikshacom.com",
    "localhost",
    "127.0.0.1",
]

CSRF_TRUSTED_ORIGINS = [
    "https://api.dev.shikshacom.com",
    "https://dev.shikshacom.com",
    "https://app.dev.shikshacom.com",
    "https://teacher.dev.shikshacom.com",
]

CORS_ALLOWED_ORIGINS = [
    "https://dev.shikshacom.com",
    "https://app.dev.shikshacom.com",
    "https://teacher.dev.shikshacom.com",
]
