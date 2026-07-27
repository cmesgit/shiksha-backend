"""
accounts/device.py — turn a request into a human-readable device label.

Used once per UserSession row (at login), never on the read path, so a small
hand-rolled matcher is preferred over pulling in a UA-parsing dependency: the
output only has to be good enough for a person to recognise their own device in
Settings → Sessions & devices and spot one they don't recognise.

Order matters in both tables below — the specific brands come before the engines
they're built on (Edge before Chrome, Chrome before Safari), because every
Chromium UA also claims to be Safari and Edge claims to be both.
"""

# (needle, label) — first match wins.
_BROWSERS = [
    ("shikshacom", "Shiksha App"),   # our own WebView/Flutter client, if it sets one
    ("edg/", "Edge"),
    ("opr/", "Opera"),
    ("samsungbrowser", "Samsung Internet"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("crios", "Chrome"),            # Chrome on iOS
    ("fxios", "Firefox"),           # Firefox on iOS
    ("safari", "Safari"),
]

_PLATFORMS = [
    ("windows nt 10", "Windows"),
    ("windows", "Windows"),
    ("android", "Android"),
    ("iphone", "iPhone"),
    ("ipad", "iPad"),
    ("mac os x", "macOS"),
    ("macintosh", "macOS"),
    ("cros", "ChromeOS"),
    ("linux", "Linux"),
]

_MOBILE_HINTS = ("iphone", "android", "mobile", "windows phone")
_TABLET_HINTS = ("ipad", "tablet")


def _first_match(ua_lower, table, default=""):
    for needle, label in table:
        if needle in ua_lower:
            return label
    return default


def parse_user_agent(user_agent):
    """→ (browser_label, platform_label, device_kind).

    An Android UA containing "mobile" is a phone; one without it is a tablet —
    that's Google's own documented convention, and the only reliable way to tell
    the two apart from the UA string.
    """
    ua = (user_agent or "").lower()

    browser = _first_match(ua, _BROWSERS, "")
    platform = _first_match(ua, _PLATFORMS, "")

    if any(h in ua for h in _TABLET_HINTS) or ("android" in ua and "mobile" not in ua):
        kind = "tablet"
    elif any(h in ua for h in _MOBILE_HINTS):
        kind = "mobile"
    else:
        kind = "desktop"

    return browser, platform, kind


def client_ip(request):
    """The caller's IP, honouring the proxy header nginx sets in front of us.

    X-Forwarded-For is a comma-separated chain appended to by each hop, so the
    left-most entry is the original client. It is spoofable by the client, but
    nginx is configured to overwrite rather than append, so the value we see is
    the one nginx observed. Returns None rather than a bogus string when nothing
    parseable is present, because the column is a GenericIPAddressField.
    """
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    candidate = forwarded or (request.META.get("REMOTE_ADDR") or "").strip()
    return candidate or None


def open_session(user, request):
    """Mint a UserSession for a fresh login.

    Called only from LoginView — every later token mint reuses the `sid` claim
    instead, so one browser stays one row for its whole life.
    """
    from .models import UserSession

    ua = request.META.get("HTTP_USER_AGENT", "")[:1000]
    browser, platform, kind = parse_user_agent(ua)
    return UserSession.objects.create(
        user=user,
        user_agent=ua,
        browser_label=browser,
        platform_label=platform,
        device_kind=kind,
        ip_address=client_ip(request),
    )


def touch_session(sid, request=None):
    """Bump `last_active_at` (and the IP, which changes on mobile networks).

    Deliberately a single targeted UPDATE with no row fetch: this runs on every
    token rotation, and the caller has no use for the object. Returns True if a
    live session matched — False means the session was revoked or never existed,
    which is how the refresh path detects a revoked session.
    """
    from django.utils import timezone

    from .models import UserSession

    if not sid:
        return False
    fields = {"last_active_at": timezone.now()}
    if request is not None:
        ip = client_ip(request)
        if ip:
            fields["ip_address"] = ip
    return UserSession.objects.filter(id=sid, revoked_at__isnull=True).update(**fields) > 0
