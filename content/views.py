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

from django.db.models import F, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .cache import LIST_TTL, list_cache_key
from django.core.cache import cache

from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
    HomeContentBlock, HomeFloater, HomeListItem, HomeSectionOrder,
    Locale, PublishStatus, ShowcaseCourse,
)
from .serializers import (
    AnnouncementSerializer, BlogPostDetailSerializer, BlogPostListSerializer,
    CurrentAffairDetailSerializer, CurrentAffairListSerializer,
    FAQItemSerializer, HomeContentBlockSerializer, HomeFloaterSerializer,
    HomeListItemSerializer, HomeSectionOrderSerializer, ShowcaseCourseSerializer,
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
        return qs.distinct()


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
    """GET /api/content/announcements/ — currently-live only."""

    serializer_class = AnnouncementSerializer
    pagination_class = None

    def get_queryset(self):
        return Announcement.objects.live()


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
