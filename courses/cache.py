# Instant list-cache invalidation without cache.clear(), mirroring
# content/cache.py's version-counter pattern: every tracked model edit bumps
# one integer ("the courses version"); public list responses are cached under
# keys that include that integer, so stale entries are simply never read
# again and expire on their own TTL.
#
# Works on any Django cache backend (LocMem in dev, Redis in prod).

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save

VERSION_KEY = "courses:version"
LIST_TTL = 300  # seconds; freshness ceiling if a bump is ever missed


def courses_version():
    v = cache.get(VERSION_KEY)
    if v is None:
        cache.set(VERSION_KEY, 1, None)
        return 1
    return v


def bump_courses_version():
    try:
        cache.incr(VERSION_KEY)
    except ValueError:  # key missing/expired
        cache.set(VERSION_KEY, 1, None)


def list_cache_key(request):
    """Stable key for a public list response: version + path + sorted query."""
    qs = "&".join(sorted(request.GET.urlencode().split("&")))
    return f"courses:list:v{courses_version()}:{request.path}?{qs}"


def _register():
    # Imported lazily so this module can load before the app registry is
    # fully ready (apps.py imports us inside ready()).
    from .models import (
        Batch, Board, Chapter, Course, CourseCategory, CourseDetail,
        Stream, Subject,
    )
    # Cross-app on purpose: /courses/public/featured/ lives in this app and
    # serves content.ShowcaseCourse rows, so a card edit has to bump THIS
    # version. ShowcaseCategory rides along for the same reason — the featured
    # payload now carries the filter-tab list, so renaming or hiding a tab must
    # invalidate it immediately rather than waiting out the 300s TTL.
    from content.models import ShowcaseCategory, ShowcaseCourse

    tracked = (
        Course, Subject, Chapter, Batch, Board,
        CourseDetail, CourseCategory, Stream, ShowcaseCourse, ShowcaseCategory,
    )

    def _bump(*args, **kwargs):
        bump_courses_version()

    for model in tracked:
        post_save.connect(_bump, sender=model, weak=False,
                          dispatch_uid=f"courses-bump-save-{model.__name__}")
        post_delete.connect(_bump, sender=model, weak=False,
                            dispatch_uid=f"courses-bump-del-{model.__name__}")


_register()
