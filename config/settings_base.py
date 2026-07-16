# config/settings_base.py
import os
from pathlib import Path
from datetime import timedelta
from corsheaders.defaults import default_headers
from dotenv import load_dotenv
MATERIAL_MAX_FILE_SIZE_MB = int(os.getenv("MATERIAL_MAX_FILE_SIZE_MB", "50"))
# CC-012 (Communication Center closure — Stage C): a chat attachment is
# exchanged in a live conversation, not a course's study-material library,
# so its ceiling is deliberately much smaller than MATERIAL_MAX_FILE_SIZE_MB.
CHAT_MAX_ATTACHMENT_MB = int(os.getenv("CHAT_MAX_ATTACHMENT_MB", "15"))

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-fallback")
DEBUG = os.getenv("DJANGO_DEBUG", "False") == "True"

AUTH_USER_MODEL = "accounts.User"

# Password strength is enforced wherever validate_password() is called
# (signup, change-password, password reset). Without this, those checks are
# no-ops. Min length 8 matches the frontend's client-side guard.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'accounts.apps.AccountsConfig',
    "courses",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "enrollments",
    "payments",
    "assignments",
    "quizzes",
    "materials",
    "django_extensions",
    "livestream.apps.LivestreamConfig",
    "dashboard",
    "activity.apps.ActivityConfig",
    "sessions_app",
    "channels",
    "skills",
    "global_settings",
    "chat",
    "forum",
    "notifications",
    "content",
    "news",
    # counseling has migrations, seed data and mounted URLs but was
    # missing here (likely a server-side settings edit that never made it
    # back to the repo — see settings.py.save.1). Required by
    # notifications.tasks.send_session_reminders.
    "counseling",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASSWORD'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "accounts.authentication.CookieJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "login": "20/min",
        "signup": "10/hour",
        "resend_verification": "3/hour",
        "password_reset_request": "5/hour",
        "password_reset_verify": "10/hour",
    },
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SAMESITE = "None"
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", ".shikshacom.com")
SESSION_COOKIE_DOMAIN = COOKIE_DOMAIN
CSRF_COOKIE_DOMAIN = COOKIE_DOMAIN

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = list(default_headers) + ["authorization"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "ERROR"},
}

# --- Email (Resend HTTPS API) ---
# Uses port 443, so it works on hosts where outbound SMTP is blocked.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Shiksha <onboarding@resend.dev>")

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
BUNNY_LIBRARY_ID = os.getenv("BUNNY_LIBRARY_ID")
BUNNY_API_KEY = os.getenv("BUNNY_API_KEY")
BUNNY_CDN_HOST = os.getenv("BUNNY_CDN_HOST", "")
BUNNY_STREAM_URL = os.getenv("BUNNY_STREAM_URL", "https://video.bunnycdn.com")
BUNNY_EMBED = os.getenv("BUNNY_EMBED", "https://iframe.mediadelivery.net/embed")
ASGI_APPLICATION = "config.asgi.application"
# ── Redis DB layout (M0 — Phase 3 §25/§32) ─────────────
# Previously channels used the implicit default db (0) and Celery used db 1,
# with no dedicated space for anything else. Made explicit here + a third db
# reserved for platform use (chat unread counters, rate limiting; presence
# later) so a maintenance flush of one concern can never touch another.
REDIS_CHANNELS_URL = os.getenv("REDIS_CHANNELS_URL", "redis://127.0.0.1:6379/0")
REDIS_PLATFORM_URL = os.getenv("REDIS_PLATFORM_URL", "redis://127.0.0.1:6379/2")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_CHANNELS_URL]},
    },
}
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")

# ── Payments ────────────────────────────────────────
# The live payment mode is normally read from the GlobalSettings singleton
# (admin-toggleable, no restart). This env var is only the fallback used
# before that table exists / is migrated. Values: free | manual_upi | razorpay
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "free")
# ── Celery ──────────────────────────────────────────
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Kolkata"
CELERY_TASK_ALWAYS_EAGER = False

# ── Notifications: SMS / reminders (see notifications/policy.py) ──────────
# Provider: console (dev, logs only) | msg91 (production, DLT-native)
#           | twilio (international fallback)
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console")
SMS_DEFAULT_COUNTRY_CODE = "+91"

MSG91_AUTH_KEY = os.getenv("MSG91_AUTH_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")

# Every "text" below must MATCH ITS DLT-REGISTERED TEMPLATE 1:1 with
# {#var#} → {name}. Register on your operator's DLT portal, create the
# matching Flow on MSG91, and drop each flow id into the env. The console
# provider renders "text" directly, so dev works with zero registration.
SMS_TEMPLATES = {
    "booking_confirmed": {
        "text": "Your ShikshaCom session {title} is confirmed for {when}. See app for details. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_BOOKING_CONFIRMED", ""),
    },
    "booking_cancelled": {
        "text": "Your ShikshaCom session {title} on {when} was cancelled. Open the app to rebook. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_BOOKING_CANCELLED", ""),
    },
    "booking_rescheduled": {
        "text": "Your ShikshaCom session {title} was rescheduled to {when}. Confirm in the app. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_BOOKING_RESCHEDULED", ""),
    },
    "session_reminder": {
        "text": "Reminder: {title} starts at {when}. Join from the ShikshaCom app. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_SESSION_REMINDER", ""),
    },
    "payment_receipt": {
        "text": "ShikshaCom: payment of Rs.{amount} received. Ref {ref}. Receipt emailed. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_PAYMENT_RECEIPT", ""),
    },
    "enrollment_approved": {
        "text": "Your ShikshaCom enrollment for {course} is approved. Start learning in the app. -SHIKSHACOM",
        "msg91_flow_id": os.getenv("MSG91_FLOW_ENROLLMENT_APPROVED", ""),
    },
}

# Reminder sweep: offsets (minutes before start) and how often beat runs
# it. Offsets ≥180 min are treated as the "24h" tier (email+push);
# smaller ones as the "1h" tier (SMS+push) — see notifications/tasks.py.
NOTIFY_REMINDER_OFFSETS_MIN = [1440, 60]
NOTIFY_REMINDER_SWEEP_MINUTES = 5

CELERY_BEAT_SCHEDULE = globals().get("CELERY_BEAT_SCHEDULE", {})
CELERY_BEAT_SCHEDULE["notifications-session-reminders"] = {
    "task": "notifications.send_session_reminders",
    "schedule": NOTIFY_REMINDER_SWEEP_MINUTES * 60,
}
