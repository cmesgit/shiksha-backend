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
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .cache import LIST_TTL, list_cache_key
from django.core.cache import cache

from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem, HeroBanner,
    HomeCategory, HomeCta, ShowcaseCourse,
)
from .serializers import (
    AnnouncementSerializer, BlogPostDetailSerializer, BlogPostListSerializer,
    CurrentAffairDetailSerializer, CurrentAffairListSerializer,
    FAQItemSerializer, HeroBannerSerializer, HomeCategorySerializer,
    HomeCtaSerializer, ShowcaseCourseSerializer,
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
             ?featured=1     ?q=resources        &page= &page_size=
    """

    serializer_class = BlogPostListSerializer

    def get_queryset(self):
        qs = (
            BlogPost.objects.published()
            .prefetch_related(Prefetch("tags", ContentTag.objects.only("name")))
        )
        p = self.request.query_params
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
    """GET /api/content/blogs/<path:slug>/ — path-style slugs supported."""

    permission_classes = [AllowAny]
    serializer_class = BlogPostDetailSerializer
    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    def get_queryset(self):
        return BlogPost.objects.published().select_related("author")

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
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
        qs = FAQItem.objects.filter(is_active=True)
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
        return ShowcaseCourse.objects.filter(is_active=True)


# ── Hero banner / home categories / closing CTA ───────────────────

class SingletonContentView(generics.GenericAPIView):
    """Base for a homepage section backed by a single active row (Hero,
    closing CTA) — cached the same way CachedListAPIView caches lists, but
    returns one object (or 204 if nothing is configured yet)."""

    permission_classes = [AllowAny]

    def get_object_or_none(self):
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached, status=200 if cached else 204)
        obj = self.get_object_or_none()
        data = self.get_serializer(obj).data if obj else {}
        cache.set(key, data, LIST_TTL)
        return Response(data, status=200 if obj else 204)


class HeroBannerView(SingletonContentView):
    """GET /api/content/hero/ — the active hero banner."""

    serializer_class = HeroBannerSerializer

    def get_object_or_none(self):
        return HeroBanner.objects.filter(is_active=True).first()


class HomeCtaView(SingletonContentView):
    """GET /api/content/cta/ — the active closing-CTA section."""

    serializer_class = HomeCtaSerializer

    def get_object_or_none(self):
        return HomeCta.objects.filter(is_active=True).first()


class HomeCategoryListView(CachedListAPIView):
    """GET /api/content/categories/ — homepage 'Browse categories' cards."""

    serializer_class = HomeCategorySerializer
    pagination_class = None

    def get_queryset(self):
        return HomeCategory.objects.filter(is_active=True)
