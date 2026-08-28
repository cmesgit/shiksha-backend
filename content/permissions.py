# PLACEMENT: backend/content/permissions.py

from rest_framework.permissions import BasePermission


class IsContentEditor(BasePermission):
    """Staff-only gate for the CMS admin API — mirrors forum.permissions.IsForumModerator."""

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_staff


class IsStudioEditor(IsContentEditor):
    """Staff, AND the Content Studio actually switched on.

    `content_studio_enabled` was described as a real gate from the start, but
    nothing enforced it: turning it off hid the Studio nav while leaving every
    Studio endpoint reachable by any staff user. A flag that only hides its own
    front door is not a gate.

    Deliberately NOT applied to the older CMS viewsets in admin_views.py. Blog
    Posts still runs on those, and the flag is supposed to control the Studio,
    not switch the whole CMS off.
    """

    message = "The Content Studio is switched off."

    # Cached because this runs on EVERY Studio request, and GlobalSettings.load()
    # is a get_or_create — a round trip per request, and an INSERT on the very
    # first one. A flip therefore takes up to a minute to reach the API, which
    # matches the dashboard side: AdminLayout reads feature_flags from the login
    # payload, so a flag change there already needs a re-login to show up.
    CACHE_KEY = "content:studio_enabled"
    CACHE_TTL = 60

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return self.studio_enabled()

    @classmethod
    def studio_enabled(cls):
        from django.core.cache import cache

        # `is None` rather than falsy: False is a real cached answer.
        cached = cache.get(cls.CACHE_KEY)
        if cached is None:
            from global_settings.models import GlobalSettings

            cached = bool(GlobalSettings.load().content_studio_enabled)
            cache.set(cls.CACHE_KEY, cached, cls.CACHE_TTL)
        return cached
