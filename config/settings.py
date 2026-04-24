from .settings_base import *
import os

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
    "https://admin.shikshacom.com",
    "https://teacher.shikshacom.com",
]

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "https://shikshacom.com",
    "https://admin.shikshacom.com",
    "https://www.shikshacom.com",
    "https://app.shikshacom.com",
    "https://admin.shikshacom.com",
    "https://teacher.shikshacom.com",
    "https://api.shikshacom.com",
]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
