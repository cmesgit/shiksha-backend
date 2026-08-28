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
# Temporary file sharing: a chat attachment auto-expires (soft-deletes) this
# many days after upload — see chat.tasks.expire_old_attachments.
CHAT_ATTACHMENT_EXPIRY_DAYS = int(os.getenv("CHAT_ATTACHMENT_EXPIRY_DAYS", "7"))
# Forum question/post attachments — a forum upload is closer to a chat
# attachment than a course material, so it shares the smaller ceiling.
FORUM_MAX_ATTACHMENT_MB = int(os.getenv("FORUM_MAX_ATTACHMENT_MB", "15"))

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
    'django.contrib.sitemaps',
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
    "documents",
    "notifications",
    "content",
    "news",
    "scholarship.apps.ScholarshipConfig",
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
        # Reuse connections across requests instead of reconnecting each time.
        # Health checks guard against handing a request a connection Postgres
        # already dropped (e.g. after a restart or idle timeout).
        'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '60')),
        'CONN_HEALTH_CHECKS': True,
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

# Whether nginx sits in front of this process and understands
# X-Accel-Redirect (config/media_views.py's secure_media_view relies on
# it to hand private-file bytes off to nginx's internal-only location
# instead of streaming them through a Django worker). True for the real
# dev/prod deployment; settings_test overrides this to False since local
# `manage.py runserver`/test runs have no nginx in front at all.
MEDIA_SERVED_BY_NGINX = True

# Bunny Edge Storage (config/bunny_storage.py) — separate product/credentials
# from the BUNNY_* video (Stream) settings below. Falls back to local disk
# when unset, so dev/test environments without a real Bunny Storage Zone
# keep working exactly as before this was added.
BUNNY_STORAGE_ZONE = os.getenv("BUNNY_STORAGE_ZONE", "")
BUNNY_STORAGE_API_KEY = os.getenv("BUNNY_STORAGE_API_KEY", "")
BUNNY_STORAGE_HOSTNAME = os.getenv("BUNNY_STORAGE_HOSTNAME", "storage.bunnycdn.com")
# The public-facing Pull Zone hostname in front of the Storage Zone above —
# deliberately a DIFFERENT variable from BUNNY_CDN_HOST (used by the video/
# Stream code in skills/views_intro_video.py, skills/listing_intro_video.py,
# courses/views_recordings.py). Storage and Stream are separate Bunny
# products, each needs its own Pull Zone; reusing BUNNY_CDN_HOST here would
# silently break video CDN links the moment someone points it at a storage
# pull zone instead.
BUNNY_STORAGE_CDN_HOST = os.getenv("BUNNY_STORAGE_CDN_HOST", "")

_using_bunny_storage = bool(BUNNY_STORAGE_ZONE and BUNNY_STORAGE_API_KEY)
if not _using_bunny_storage:
    import warnings
    warnings.warn(
        "BUNNY_STORAGE_ZONE/BUNNY_STORAGE_API_KEY are not set — CMS media "
        "(images, uploads) will be written to local disk instead of "
        "BunnyCDN. Fine for local dev/test; if this fires on a real "
        "deployment, uploads there are not CDN-served and won't survive a "
        "redeploy that doesn't preserve the media/ directory.",
        RuntimeWarning,
        stacklevel=1,
    )

STORAGES = {
    "default": (
        {"BACKEND": "config.bunny_storage.BunnyStorage"}
        if _using_bunny_storage
        else {"BACKEND": "config.secure_local_storage.SecureLocalStorage"}
    ),
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

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
        # Per-IP. Deliberately generous: schools and families share one NAT
        # address, so a tight per-IP cap locks out a whole class at 9am while
        # an attacker with a few IPs barely notices. The per-ACCOUNT limit
        # below is what actually makes guessing one password expensive.
        "login": "20/min",
        "login_account": "10/min",
        "signup": "10/hour",
        "resend_verification": "3/hour",
        "password_reset_request": "5/hour",
        "password_reset_verify": "10/hour",
        # PIN guesses on profile-switch — a 4-6 digit PIN with no rate
        # limit at all is brute-forceable within seconds.
        "pin_verify": "10/min",
        # Forum anti-abuse: cap how fast a single user can create content
        # or file reports (spam / flood protection).
        "forum_post": "20/hour",
        "forum_comment": "60/hour",
        "forum_report": "30/hour",
        # Explore library anti-abuse.
        "documents_upload": "30/hour",
        "documents_report": "30/hour",
        # Quiz builder's AI question drafting — costs real money per call.
        "quiz_ai_generate": "10/hour",
        # Scholarship question-bank AI drafting — same reasoning as above.
        "scholarship_ai_generate": "10/hour",
        # General Studies page's AI assist — public, no auth gate, so this
        # is the only thing standing between an anonymous visitor and
        # unlimited OpenAI spend on our key.
        "general_studies_ai": "5/hour",
        # Anonymous "notify me when {board} launches" lead capture — the
        # only unauthenticated write endpoint in the app, so throttled hard.
        "board_notify": "5/hour",
        # Public agreement-letter GET (signup screen) — was the only AllowAny
        # endpoint in accounts/ with no throttle at all.
        "agreement_public": "60/hour",
        # Public faculty-signup option lists (static taxonomy, no user data).
        "faculty_choices": "120/hour",
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

# The marketing site's own domain (not this API's), for building absolute
# frontend URLs from backend code — e.g. sitemap.xml, whose <loc> entries
# must point at the frontend host, never at api.shikshacom.com.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://www.shikshacom.com")

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

# Quiz builder's "Generate with AI" action (quizzes/views.py
# TeacherGenerateAIQuestionsView). Unset by default — the endpoint raises a
# clear RuntimeError until an operator adds a real key to the environment.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Scholarship eligibility dedup (scholarship/services.py compute_dedup_hash).
# A server-side pepper mixed into the hash of {guardian verification
# reference, child name, child DOB} so the stored hash isn't a bare,
# realistically-reversible digest of low-entropy identity data. Falls back to
# SECRET_KEY so this works out of the box in dev/tests; production should set
# a dedicated value so rotating SECRET_KEY doesn't also reshuffle every
# existing eligibility record's hash.
SCHOLARSHIP_DEDUP_PEPPER = os.getenv("SCHOLARSHIP_DEDUP_PEPPER", "") or SECRET_KEY

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
BUNNY_LIBRARY_ID = os.getenv("BUNNY_LIBRARY_ID")
BUNNY_API_KEY = os.getenv("BUNNY_API_KEY")
BUNNY_CDN_HOST = os.getenv("BUNNY_CDN_HOST", "")
BUNNY_STREAM_URL = os.getenv("BUNNY_STREAM_URL", "https://video.bunnycdn.com")
BUNNY_EMBED = os.getenv("BUNNY_EMBED", "https://iframe.mediadelivery.net/embed")
# Bunny Stream "Embed View Token Authentication" key — a THIRD Bunny secret,
# distinct from BUNNY_API_KEY (uploads) and BUNNY_STORAGE_API_KEY (files).
# Found in the Stream dashboard under Library → Security → Embed View Token
# Authentication. config/bunny_signing.py uses it to sign short-lived iframe
# playback URLs so a copied embed link stops working; without it every
# recording URL is permanent and unauthenticated (see that module's docstring).
# Signing alone is not enforcement: the same library setting must also be
# switched ON in Bunny, or unsigned URLs keep working.
BUNNY_STREAM_TOKEN_KEY = os.getenv("BUNNY_STREAM_TOKEN_KEY", "")

# ── LiveKit Egress → Bunny Edge Storage (automatic class recording) ────────
#
# Phase 0 of automatic class recording. LiveKit Cloud's egress service
# composites the live room and writes the result straight into a Bunny
# Storage Zone over Bunny's S3-compatible API; a later phase pulls that file
# into Bunny Stream, which is the product the entire existing
# SessionRecording playback path already speaks (courses/views_recordings.py,
# config/bunny_signing.py). Storage is the drop box, Stream stays the
# library — nothing downstream of the fetch hop changes.
#
# These are deliberately a FOURTH, separate set of Bunny credentials,
# distinct from:
#   • BUNNY_STORAGE_*        — the CMS image bucket (config/bunny_storage.py)
#   • BUNNY_API_KEY          — Bunny Stream library uploads
#   • BUNNY_STREAM_TOKEN_KEY — Bunny Stream embed-token signing
#
# The separation is not tidiness, it is the whole safety story:
#   1. The CMS storage zone is shared between dev and prod. Pointing egress
#      at it would drop dev's test recordings into the bucket prod serves
#      publicly, which is exactly the accident this split prevents.
#   2. Raw class video has to sit briefly on a PUBLIC pull zone so the Bunny
#      Stream fetch hop can read it (Bunny's POST /videos/fetch takes a plain
#      URL and cannot use a signed one). That is a property you never want
#      the CMS bucket to have.
BUNNY_EGRESS_ZONE = os.getenv("BUNNY_EGRESS_ZONE", "")
# The Storage Zone password, used as the S3 secret access key. Bunny has no
# separate access-key concept: the zone NAME above is the access key id, and
# the bucket name, all three at once (Access → S3 tab in the dashboard).
BUNNY_EGRESS_API_KEY = os.getenv("BUNNY_EGRESS_API_KEY", "")
# Bunny Storage region code of the zone: de (Frankfurt), ny, sg, uk, se, la,
# jh or syd. This is BOTH the SigV4 signing region and the endpoint's own
# subdomain, so it is stored once and the host derived from it below — the
# two disagreeing is a silent misconfiguration that surfaces only as an
# opaque signature error on the first real recording.
#
# Lower-cased deliberately. Bunny's two APIs disagree on the casing of their
# own region codes: the zone-creation API takes "SG" and the dashboard
# displays "SG", but the S3 API signs with "sg". DNS would forgive the
# hostname, SigV4 will not, so copying the code straight off the dashboard
# into this variable produced a signature failure with nothing in the message
# to suggest capitalisation was the cause.
BUNNY_EGRESS_REGION = os.getenv("BUNNY_EGRESS_REGION", "de").strip().lower()
# S3-compatible endpoint host. NOTE this is NOT the same host as the native
# Edge Storage API that config/bunny_storage.py talks to
# (storage.bunnycdn.com): the S3 API answers on
# "<region>-s3.storage.bunnycdn.com", and pointing egress at the native host
# fails to authenticate. The override exists only for the case where Bunny's
# dashboard shows a host that doesn't match this pattern — read it off the
# zone's Access → S3 tab rather than guessing.
BUNNY_EGRESS_S3_HOST = (
    os.getenv("BUNNY_EGRESS_S3_HOST")
    or f"{BUNNY_EGRESS_REGION}-s3.storage.bunnycdn.com"
)
# NATIVE Edge Storage API host for this zone — a THIRD host, distinct from
# both BUNNY_EGRESS_S3_HOST above and BUNNY_STORAGE_HOSTNAME (which belongs to
# the CMS zone and may be in a different region entirely). Used only to DELETE
# the raw mp4 once Bunny Stream has ingested it: the native API takes a simple
# AccessKey header, so purging needs no SigV4 signing and therefore no boto3
# in the app's dependencies.
#
# Bunny's native host pattern gives the main region no prefix and every other
# region one: storage.bunnycdn.com for de, sg.storage.bunnycdn.com for sg.
BUNNY_EGRESS_STORAGE_HOST = os.getenv("BUNNY_EGRESS_STORAGE_HOST") or (
    "storage.bunnycdn.com" if BUNNY_EGRESS_REGION == "de"
    else f"{BUNNY_EGRESS_REGION}.storage.bunnycdn.com"
)
# Public Pull Zone hostname in front of BUNNY_EGRESS_ZONE, read only by the
# Bunny Stream fetch hop. Must be its OWN pull zone serving only this bucket
# — see reason (2) above. Deliberately not BUNNY_STORAGE_CDN_HOST and not
# BUNNY_CDN_HOST.
BUNNY_EGRESS_PULL_HOST = os.getenv("BUNNY_EGRESS_PULL_HOST", "")
# Key prefix inside the zone. Object keys are
# "<prefix>/<session_id>/<random>.mp4" — the random segment is load-bearing
# rather than cosmetic: between egress finishing and the Stream fetch
# completing, that object is readable by anyone who can guess its URL.
BUNNY_EGRESS_PREFIX = os.getenv("BUNNY_EGRESS_PREFIX", "class-egress")

_egress_requested = os.getenv("LIVEKIT_EGRESS_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)
_egress_creds = all([
    LIVEKIT_URL,
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    BUNNY_EGRESS_ZONE,
    BUNNY_EGRESS_API_KEY,
])

# The single flag every egress code path checks. False unless recording was
# explicitly switched ON *and* every credential it needs is present, so the
# test settings (which set no LIVEKIT_* at all — see the note in CLAUDE.md)
# and any half-configured deploy keep the existing manual-upload behaviour
# instead of raising on every teacher join.
LIVEKIT_EGRESS_ENABLED = _egress_requested and _egress_creds

if _egress_requested and not _egress_creds:
    import warnings
    warnings.warn(
        "LIVEKIT_EGRESS_ENABLED is set but LiveKit and/or Bunny egress "
        "credentials are missing — automatic class recording stays OFF. "
        "Needs LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, "
        "BUNNY_EGRESS_ZONE and BUNNY_EGRESS_API_KEY.",
        RuntimeWarning,
        stacklevel=1,
    )

# Hard stop rather than a warning: sharing a Storage Zone with the CMS
# bucket is never a valid configuration, and the failure it produces (dev's
# test recordings served from prod's public CDN) is silent, and already
# irreversible by the time anyone notices. Fail at import instead.
if BUNNY_EGRESS_ZONE and BUNNY_EGRESS_ZONE == BUNNY_STORAGE_ZONE:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "BUNNY_EGRESS_ZONE must not be the same Bunny Storage Zone as "
        "BUNNY_STORAGE_ZONE. Automatic class recordings need their own zone "
        "and their own pull zone — see config/settings_base.py for why."
    )

# Razorpay — referenced by payments/services.py (order creation, at module
# import time) and payments/webhooks.py (signature verification) but never
# actually defined here before now; both would raise AttributeError the
# moment either code path ran. Dormant while GlobalSettings.payment_mode
# is "free"/"manual_upi", but must exist so switching to "razorpay" doesn't
# immediately break checkout and payment confirmation.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

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
# Default cache on the platform Redis db. DRF throttling uses this cache, so
# rate limits are now shared across uvicorn workers instead of per-process
# LocMem counters that reset on every restart. KEY_PREFIX keeps Django's
# cache keys out of the way of the raw platform keys (chat unread counters)
# that share db2.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_PLATFORM_URL,
        "KEY_PREFIX": "djcache",
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
