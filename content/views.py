# PLACEMENT: backend/content/views.py
#
# Public, read-only endpoints. The project's DRF default is
# IsAuthenticated (cookie-JWT), so every view here sets AllowAny
# explicitly. Writes happen only through Django admin.
#
# Caching strategy:
#   • List endpoints — cached under a version-keyed cache key
#     (content/cache.py). Any admin edit bumps the version, so caches
#     invalidate instantly; TTL is only a safety net.
#   • Detail endpoints — not cached: single-row unique-index lookups, and
#     blog detail increments view_count.

from django.conf import settings
from django.db.models import F, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .cache import LIST_TTL, list_cache_key
from django.core.cache import cache

from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, DemoVideo, FAQItem,
    HomeContentBlock, HomeFloater, HomeListItem, HomeSectionOrder,
    Locale, PublishStatus, ShowcaseCourse, TickerSlot,
)
from .serializers import (
    AnnouncementSerializer, BlogPostDetailSerializer, BlogPostListSerializer,
    CurrentAffairDetailSerializer, CurrentAffairListSerializer,
    DemoVideoSerializer, FAQItemSerializer, HomeContentBlockSerializer,
    HomeFloaterSerializer, HomeListItemSerializer, HomeSectionOrderSerializer,
    ShowcaseCourseSerializer,
)


class ContentPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = "page_size"
    max_page_size = 50


class CachedListAPIView(generics.ListAPIView):
    """ListAPIView whose full JSON response is memoised per
    (content version, path, query-string)."""

    permission_classes = [AllowAny]
    pagination_class = ContentPagination

    def list(self, request, *args, **kwargs):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)
        response = super().list(request, *args, **kwargs)
        cache.set(key, response.data, LIST_TTL)
        return response


# ── Blog ──────────────────────────────────────────────────────────

class BlogPostListView(CachedListAPIView):
    """GET /api/content/blogs/
    Filters: ?class_level=9  ?subject=economics  ?tag=ncert
             ?featured=1     ?q=resources        ?locale=hi (default en)
             &page= &page_size=

    No cross-locale merge/fallback here (that's a detail-page-only concept,
    see BlogPostDetailView) — a `?locale=hi` listing simply returns however
    many Hindi rows exist today; fewer cards than the English list until
    more translations are added is expected, not a bug.
    """

    serializer_class = BlogPostListSerializer

    def get_queryset(self):
        p = self.request.query_params
        qs = (
            BlogPost.objects.published()
            .filter(locale=p.get("locale", Locale.EN))
            .prefetch_related(Prefetch("tags", ContentTag.objects.only("name")))
        )
        if p.get("class_level"):
            qs = qs.filter(class_level=p["class_level"])
        if p.get("subject"):
            qs = qs.filter(subject=p["subject"])
        if p.get("tag"):
            qs = qs.filter(tags__slug=p["tag"])
        if p.get("featured"):
            qs = qs.filter(is_featured=True)
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(excerpt__icontains=q)
                | Q(slug__icontains=q)
            )
        return qs.distinct().order_by(*self._ordering(p.get("sort")))

    # Whitelisted, because an open `?ordering=` on a public endpoint lets a
    # caller sort by any column including ones the serializer never exposes.
    SORTS = {
        "newest":   ("-publish_at",),
        "oldest":   ("publish_at",),
        "popular":  ("-view_count", "-publish_at"),
        "title":    ("title", "-publish_at"),
    }

    def _ordering(self, sort):
        """Default stays `-publish_at`, which is what Meta.ordering gave and
        what every existing caller already relies on. An unknown value falls
        back to it rather than 400ing — a stale bookmark should not break the
        listing. The secondary key makes the order total: `popular` ties at
        view_count=0 for most rows, and an unstable sort reshuffles the page
        on every request, which paginates badly."""
        return self.SORTS.get((sort or "").strip().lower(), self.SORTS["newest"])


class BlogPostDetailView(generics.RetrieveAPIView):
    """GET /api/content/blogs/<path:slug>/?locale=hi — path-style slugs
    supported. Falls back to the English row for the same slug (never a
    404, never a silent redirect) when the requested locale has no
    translation yet — the response's `is_fallback_locale` flag lets the
    frontend show a "not yet translated" banner while keeping the
    originally-requested URL intact."""

    permission_classes = [AllowAny]
    serializer_class = BlogPostDetailSerializer
    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    def get_queryset(self):
        locale = self.request.query_params.get("locale", Locale.EN)
        return BlogPost.objects.published().filter(locale=locale).select_related("author")

    def retrieve(self, request, *args, **kwargs):
        locale = request.query_params.get("locale", Locale.EN)
        slug = self.kwargs[self.lookup_url_kwarg]
        try:
            instance = self.get_object()
        except Http404:
            if locale == Locale.EN:
                raise
            fallback_qs = (
                BlogPost.objects.published().filter(locale=Locale.EN, slug=slug)
                .select_related("author")
            )
            instance = get_object_or_404(fallback_qs, slug=slug)
            instance.is_fallback_locale = True
        BlogPost.objects.filter(pk=instance.pk).update(
            view_count=F("view_count") + 1
        )
        return Response(self.get_serializer(instance).data)


class BlogPostRelatedView(generics.GenericAPIView):
    """GET /api/content/blogs/<path:slug>/related/?locale=

    An article page had no way out but the Back button: no related posts, no
    next/previous chapter. The data model already supported both cheaply —
    `chapter_number` plus the `(class_level, subject)` index — and nothing
    used it.

    Returns three things, because they answer different questions:

        prev / next — the chapters either side of this one in the SAME
                      class_level + subject. This is the spine of a textbook
                      series and is what a reader working through Class 9
                      Science actually wants.
        related     — up to four more posts from the same class_level +
                      subject, nearest chapter first, excluding prev/next and
                      the post itself.

    `prev`/`next` are null for a post with no `chapter_number` (most
    non-series posts), and `related` degrades to same-subject-any-class
    rather than returning nothing, so the foot of the page is never empty
    for a post that has siblings.
    """

    permission_classes = [AllowAny]

    RELATED_LIMIT = 4

    def get(self, request, slug):
        locale = request.query_params.get("locale", Locale.EN)
        base = BlogPost.objects.published().filter(locale=locale)
        post = get_object_or_404(base, slug=slug)

        siblings = base.filter(
            class_level=post.class_level, subject=post.subject,
        ).exclude(pk=post.pk)

        prev_post = next_post = None
        if post.chapter_number is not None:
            numbered = siblings.filter(chapter_number__isnull=False)
            prev_post = (numbered
                         .filter(chapter_number__lt=post.chapter_number)
                         .order_by("-chapter_number").first())
            next_post = (numbered
                         .filter(chapter_number__gt=post.chapter_number)
                         .order_by("chapter_number").first())

        used = {p.pk for p in (prev_post, next_post) if p}
        related = list(siblings.exclude(pk__in=used)
                       .order_by("chapter_number", "-publish_at")
                       [: self.RELATED_LIMIT])

        # A post that is the only one in its class+subject still deserves a
        # footer. Widen to the same subject across class levels before
        # giving up — a Class 9 chemistry reader is plausibly served by a
        # Class 10 chemistry post, and an empty row is served by nothing.
        if not related:
            related = list(
                base.filter(subject=post.subject)
                .exclude(pk=post.pk).exclude(pk__in=used)
                .order_by("-publish_at")[: self.RELATED_LIMIT]
            )

        ser = BlogPostListSerializer
        ctx = self.get_serializer_context()
        return Response({
            "prev": ser(prev_post, context=ctx).data if prev_post else None,
            "next": ser(next_post, context=ctx).data if next_post else None,
            "related": ser(related, many=True, context=ctx).data,
        })


# ── Current affairs ───────────────────────────────────────────────

class CurrentAffairListView(CachedListAPIView):
    """GET /api/content/current-affairs/
    Filters: ?category=economy ?date=2026-07-01 ?month=2026-07 ?q=
    """

    serializer_class = CurrentAffairListSerializer

    def get_queryset(self):
        qs = CurrentAffair.objects.published().prefetch_related("tags")
        p = self.request.query_params
        if p.get("category"):
            qs = qs.filter(category=p["category"])
        if p.get("date"):
            qs = qs.filter(affair_date=p["date"])
        if p.get("month"):  # YYYY-MM
            try:
                year, month = p["month"].split("-")
                qs = qs.filter(affair_date__year=int(year),
                               affair_date__month=int(month))
            except (ValueError, AttributeError):
                pass
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q))
        return qs.distinct()


class CurrentAffairDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = CurrentAffairDetailSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return CurrentAffair.objects.published()


# ── FAQ / announcements / showcase ────────────────────────────────

class FAQListView(CachedListAPIView):
    """GET /api/content/faqs/?page_key=home (unpaginated: FAQs are short)."""

    serializer_class = FAQItemSerializer
    pagination_class = None

    def get_queryset(self):
        qs = FAQItem.objects.filter(status=PublishStatus.PUBLISHED)
        page_key = self.request.query_params.get("page_key")
        if page_key:
            qs = qs.filter(page=page_key)
        return qs


class AnnouncementListView(CachedListAPIView):
    """GET /api/content/announcements/[?slot=] — currently-live only.

    `slot` defaults to **navbar**, which is what keeps the existing strip
    working unchanged: the site-wide strip has always called this endpoint
    with no parameters, and must keep getting exactly the rows it always did.
    Every other ticker surface passes its own slot.

    Caching is safe here — `list_cache_key` (content/cache.py:34-37) already
    folds the sorted query string into the key, so `?slot=hero` cannot be
    served a cached navbar response.
    """

    serializer_class = AnnouncementSerializer
    pagination_class = None

    def get_queryset(self):
        slot = self.request.query_params.get("slot") or TickerSlot.NAVBAR
        if slot not in TickerSlot.values:
            # 400 rather than an empty list. A typo'd slot returning [] is
            # indistinguishable from "nothing is scheduled here", which is the
            # same failure mode as admin.js's safe() turning an outage into
            # empty content — the bug that once read as "there is no content".
            raise DRFValidationError({
                "slot": f"Unknown slot '{slot}'. Valid: "
                        f"{', '.join(TickerSlot.values)}.",
            })
        return Announcement.objects.for_slot(slot)


class ShowcaseListView(CachedListAPIView):
    """GET /api/content/showcase/ — homepage featured-course cards."""

    serializer_class = ShowcaseCourseSerializer
    pagination_class = None

    def get_queryset(self):
        return ShowcaseCourse.objects.filter(status=PublishStatus.PUBLISHED)


# ── Homepage content ───────────────────────────────────────────────

class HomeContentListView(CachedListAPIView):
    """GET /api/content/home-content/?section=hero"""

    serializer_class = HomeContentBlockSerializer
    pagination_class = None

    def get_queryset(self):
        qs = HomeContentBlock.objects.filter(status=PublishStatus.PUBLISHED)
        section = self.request.query_params.get("section")
        if section:
            qs = qs.filter(section=section)
        return qs


class HomeListItemListView(CachedListAPIView):
    """GET /api/content/home-list-items/?section=why_shiksha"""

    serializer_class = HomeListItemSerializer
    pagination_class = None

    def get_queryset(self):
        qs = HomeListItem.objects.filter(status=PublishStatus.PUBLISHED)
        p = self.request.query_params
        if p.get("section"):
            qs = qs.filter(section=p["section"])
        if p.get("variant"):
            qs = qs.filter(variant=p["variant"])
        return qs


class HomeFloaterListView(CachedListAPIView):
    """GET /api/content/home-floaters/?section=why_choose"""

    serializer_class = HomeFloaterSerializer
    pagination_class = None

    def get_queryset(self):
        qs = HomeFloater.objects.filter(status=PublishStatus.PUBLISHED)
        section = self.request.query_params.get("section")
        if section:
            qs = qs.filter(section=section)
        return qs


class HomeSectionOrderListView(CachedListAPIView):
    """GET /api/content/home-section-order/ — ordered, visible-only list of
    homepage sections, so the public site can render sections in whatever
    sequence the admin has configured instead of a hardcoded JSX order."""

    serializer_class = HomeSectionOrderSerializer
    pagination_class = None

    def get_queryset(self):
        return HomeSectionOrder.objects.filter(is_visible=True)


class DemoVideoListView(CachedListAPIView):
    """GET /api/content/demo-videos/ — the landing page's walkthrough clips.

    Excludes rows with no ``bunny_video_id``. Published-but-unconfigured is a
    real state (an admin creates the row, then uploads), and a row that reaches
    the frontend without a video renders a play button opening an empty player.
    Filtering here means the site's own "is there anything to show" check is
    just ``list.length``, so before any clip is uploaded the floating button
    does not render at all rather than rendering broken.

    Also excludes rows Bunny has not finished encoding. A guid proves a slot
    was created, not that it holds anything: on 2026-09-11 two uploads returned
    success, stored zero bytes, and stuck at status 2 — so the homepage offered
    two demos that opened an empty player. Requiring ``bunny_status == 4``
    closes the second route to the exact failure the guid check was written to
    prevent. The cost is that a freshly-pasted guid stays invisible until
    ``sync_demo_videos`` runs, which is the safe direction: unknown means
    hidden.
    """

    serializer_class = DemoVideoSerializer
    pagination_class = None

    def get_queryset(self):
        # BUNNY_LIBRARY_ID has no default in settings_base and is None when the
        # environment does not set it, which makes embed_url() None for every
        # row. Serving those would hand the frontend a menu of videos that
        # cannot play. Checked here rather than per-row because it is a
        # process-wide setting: either the whole library is configured or none
        # of it is.
        if not settings.BUNNY_LIBRARY_ID:
            return DemoVideo.objects.none()
        return (
            DemoVideo.objects
            .filter(status=PublishStatus.PUBLISHED,
                    bunny_status=DemoVideo.BUNNY_FINISHED)
            .exclude(bunny_video_id="")
        )
