# PLACEMENT: backend/content/admin_views.py
#
# Staff-only CRUD API for the CMS admin UI (companion React app). Plain
# `viewsets.ModelViewSet`s — every resource here is straightforward CRUD, so
# there's no need for the bespoke APIView style forum/moderation_views.py
# uses. Django admin (content/admin.py) stays as a parallel, independent
# editing surface; this is unrelated and doesn't touch it.
#
# Pagination: bumped page_size to 20 (vs. the public API's 12, see
# content/views.py's ContentPagination) — editors scan more rows per page
# than the public site's cards; still capped at 50 via ?page_size=.

from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from .admin_serializers import (
    AnnouncementAdminSerializer, BlogPostAdminSerializer,
    ContentTagSerializer, CurrentAffairAdminSerializer,
    FAQItemAdminSerializer, HomeContentBlockAdminSerializer,
    HomeFloaterAdminSerializer, HomeListItemAdminSerializer,
    ShowcaseCourseAdminSerializer,
)
from .models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
    HomeContentBlock, HomeFloater, HomeListItem, PublishStatus, ShowcaseCourse,
)
from .permissions import IsContentEditor


class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50


def _sync_tags(instance, tag_names):
    """get_or_create ContentTag rows for each name and set the M2M —
    mirrors forum/moderation_views.py's tag handling."""
    if tag_names is None:
        return
    tags = [
        ContentTag.objects.get_or_create(name=name.strip().lower())[0]
        for name in tag_names if name and name.strip()
    ]
    instance.tags.set(tags)


# ── Tags ──────────────────────────────────────────────────────────

class TagAdminViewSet(viewsets.ModelViewSet):
    queryset = ContentTag.objects.all()
    serializer_class = ContentTagSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(name__icontains=q)
        return qs


# ── FAQ ───────────────────────────────────────────────────────────

class FAQItemAdminViewSet(viewsets.ModelViewSet):
    queryset = FAQItem.objects.all()
    serializer_class = FAQItemAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination

    def get_queryset(self):
        qs = super().get_queryset()
        # NOTE: filter param is `page_key`, not `page` — `page` is reserved
        # by AdminPagination for page-number navigation (`?page=2`), and
        # colliding with it made `?page=home` 404 with {"detail":"Invalid
        # page."} instead of filtering. `page_key` also matches the public
        # FAQListView's existing convention (content/views.py: `?page_key=`).
        page_key = self.request.query_params.get("page_key")
        if page_key:
            qs = qs.filter(page=page_key)
        return qs


# ── Announcements ─────────────────────────────────────────────────

class AnnouncementAdminViewSet(viewsets.ModelViewSet):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination


# ── Showcase courses ──────────────────────────────────────────────

class ShowcaseCourseAdminViewSet(viewsets.ModelViewSet):
    queryset = ShowcaseCourse.objects.all()
    serializer_class = ShowcaseCourseAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    # Explicit even though DRF's global default already includes these
    # (no DEFAULT_PARSER_CLASSES override in config/settings_base.py) —
    # `image` is a file upload field, so multipart support must not be
    # left to an implicit default that could change.
    parser_classes = [MultiPartParser, FormParser, JSONParser]


# ── Homepage content ──────────────────────────────────────────────

class HomeContentBlockAdminViewSet(viewsets.ModelViewSet):
    queryset = HomeContentBlock.objects.all()
    serializer_class = HomeContentBlockAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]  # `image` upload

    def get_queryset(self):
        qs = super().get_queryset()
        section = self.request.query_params.get("section")
        if section:
            qs = qs.filter(section=section)
        return qs


class HomeListItemAdminViewSet(viewsets.ModelViewSet):
    queryset = HomeListItem.objects.all()
    serializer_class = HomeListItemAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("section"):
            qs = qs.filter(section=p["section"])
        if p.get("variant"):
            qs = qs.filter(variant=p["variant"])
        return qs


class HomeFloaterAdminViewSet(viewsets.ModelViewSet):
    queryset = HomeFloater.objects.all()
    serializer_class = HomeFloaterAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination

    def get_queryset(self):
        qs = super().get_queryset()
        section = self.request.query_params.get("section")
        if section:
            qs = qs.filter(section=section)
        return qs


# ── Blog posts ────────────────────────────────────────────────────

class BlogPostAdminViewSet(viewsets.ModelViewSet):
    queryset = BlogPost.objects.all()
    serializer_class = BlogPostAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = super().get_queryset().select_related("author").prefetch_related("tags")
        p = self.request.query_params
        if p.get("class_level"):
            qs = qs.filter(class_level=p["class_level"])
        if p.get("subject"):
            qs = qs.filter(subject=p["subject"])
        if p.get("status"):
            qs = qs.filter(status=p["status"])
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

    def perform_create(self, serializer):
        tags = serializer.validated_data.pop("tags", None)
        instance = serializer.save()
        if not instance.author_id:
            instance.author = self.request.user
            instance.save(update_fields=["author"])
        _sync_tags(instance, tags)

    def perform_update(self, serializer):
        tags = serializer.validated_data.pop("tags", None)
        instance = serializer.save()
        _sync_tags(instance, tags)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        post = self.get_object()
        post.status = PublishStatus.PUBLISHED
        post.publish_at = timezone.now()
        post.save()
        return Response(self.get_serializer(post).data)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        post = self.get_object()
        post.status = PublishStatus.DRAFT
        post.save()
        return Response(self.get_serializer(post).data)


# ── Current affairs ───────────────────────────────────────────────

class CurrentAffairAdminViewSet(viewsets.ModelViewSet):
    queryset = CurrentAffair.objects.all()
    serializer_class = CurrentAffairAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related("tags")
        p = self.request.query_params
        if p.get("category"):
            qs = qs.filter(category=p["category"])
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("affair_date"):
            qs = qs.filter(affair_date=p["affair_date"])
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(
                Q(title__icontains=q) | Q(summary__icontains=q) | Q(slug__icontains=q)
            )
        return qs.distinct()

    def perform_create(self, serializer):
        tags = serializer.validated_data.pop("tags", None)
        instance = serializer.save()
        _sync_tags(instance, tags)

    def perform_update(self, serializer):
        tags = serializer.validated_data.pop("tags", None)
        instance = serializer.save()
        _sync_tags(instance, tags)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        obj = self.get_object()
        obj.status = PublishStatus.PUBLISHED
        obj.publish_at = timezone.now()
        obj.save()
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        obj = self.get_object()
        obj.status = PublishStatus.DRAFT
        obj.save()
        return Response(self.get_serializer(obj).data)
