# PLACEMENT: backend/content/cache.py
#
# Instant list-cache invalidation without cache.clear():
# every content edit bumps one integer ("the content version"); list
# responses are cached under keys that include that integer, so stale
# entries are simply never read again and expire on their own TTL.
#
# Works on any Django cache backend (LocMem in dev, Redis in prod — see
# README_CONTENT.md for the CACHES snippet using REDIS_PLATFORM_URL).

from django.core.cache import cache
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

VERSION_KEY = "content:version"
LIST_TTL = 300  # seconds; freshness ceiling if a bump is ever missed


def content_version():
    v = cache.get(VERSION_KEY)
    if v is None:
        cache.set(VERSION_KEY, 1, None)
        return 1
    return v


def bump_content_version():
    try:
        cache.incr(VERSION_KEY)
    except ValueError:  # key missing/expired
        cache.set(VERSION_KEY, 1, None)


def list_cache_key(request):
    """Stable key for a public list response: version + path + sorted query."""
    qs = "&".join(sorted(request.GET.urlencode().split("&")))
    return f"content:list:v{content_version()}:{request.path}?{qs}"


def _register():
    # Imported lazily so this module can load before the app registry is
    # fully ready (apps.py imports us inside ready()).
    from .models import (
        Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
        ShowcaseCourse,
    )

    tracked = (
        BlogPost, CurrentAffair, FAQItem, Announcement, ShowcaseCourse,
        ContentTag,
    )

    def _bump(*args, **kwargs):
        bump_content_version()

    for model in tracked:
        post_save.connect(_bump, sender=model, weak=False,
                          dispatch_uid=f"content-bump-save-{model.__name__}")
        post_delete.connect(_bump, sender=model, weak=False,
                            dispatch_uid=f"content-bump-del-{model.__name__}")

    m2m_changed.connect(_bump, sender=BlogPost.tags.through, weak=False,
                        dispatch_uid="content-bump-m2m-blog")
    m2m_changed.connect(_bump, sender=CurrentAffair.tags.through, weak=False,
                        dispatch_uid="content-bump-m2m-ca")


_register()
