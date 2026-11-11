# PLACEMENT: backend/backend/counseling/guide_views.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/guide_views.py
#
# API surface:
#
# PUBLIC
#   GET  /counseling/guides/?audience=&q=&specialization=
#   GET  /counseling/guides/<slug>/?full=1
#   GET  /counseling/guides/<slug>/chapters/<chapter_slug>/
#   POST /counseling/guides/<slug>/view/
#
# STAFF (IsContentEditor — is_staff)
#   GET/POST        /counseling/admin/guides/
#   GET/PATCH/DELETE /counseling/admin/guides/<id>/
#   GET/POST        /counseling/admin/guides/<id>/sections/
#   PATCH/DELETE    /counseling/admin/sections/<id>/
#   POST            /counseling/admin/guides/<id>/reorder/
#   POST            /counseling/admin/guides/<id>/publish/
#
# `slug` resolution also checks `legacy_slugs` (a JSONField list), so a
# renamed guide's old URL — secondary-school replacing class-10, here —
# keeps resolving; the response's `canonical_slug` tells the client to
# rewrite the address bar rather than silently serving the old address.
#
# No DRF routers/ViewSets, per this app's existing convention — explicit
# APIView classes and a flat urls.py, same as the rest of counseling/.

from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from content.permissions import IsContentEditor

from .guide_models import CareerGuide, GuideChapter, GuideReadingProgress, GuideSection
from .guide_serializers import (
    CareerGuideAdminSerializer, CareerGuideCardSerializer,
    CareerGuideDetailSerializer, GuideChapterSerializer, GuideSectionSerializer,
)

INLINE_SECTION_LIMIT = 40


class GuidePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _resolve_guide(slug, *, published_only):
    """Match on the live slug first, then any legacy alias. Returns
    (guide, is_alias) so the caller can tell the client to rewrite the URL."""
    qs = CareerGuide.objects.published() if published_only else CareerGuide.objects.all()
    guide = qs.filter(slug=slug).first()
    if guide:
        return guide, False
    for candidate in qs:
        if slug in (candidate.legacy_slugs or []):
            return candidate, True
    return None, False


# =====================================================
# Public
# =====================================================

class GuideListView(APIView):
    permission_classes = [AllowAny]
    pagination_class = GuidePagination

    def get(self, request):
        qs = CareerGuide.objects.published().prefetch_related("specializations")

        audience = request.query_params.get("audience")
        if audience:
            qs = qs.filter(stage=audience)

        specialization = request.query_params.get("specialization")
        if specialization:
            qs = qs.filter(specializations__name=specialization)

        q = request.query_params.get("q")
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(blurb__icontains=q) | Q(audience__icontains=q)
            )

        qs = qs.distinct()
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = CareerGuideCardSerializer(page, many=True, context={"request": request}).data
        return paginator.get_paginated_response(data)


class GuideDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        guide, is_alias = _resolve_guide(slug, published_only=True)
        if guide is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        section_count = guide.sections.count()
        inline = request.query_params.get("full") == "1" or section_count <= INLINE_SECTION_LIMIT
        data = CareerGuideDetailSerializer(
            guide, context={"request": request, "inline_sections": inline}
        ).data
        data["is_alias"] = is_alias
        data["section_count"] = section_count
        return Response(data)


class GuideChapterDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug, chapter_slug):
        guide, _ = _resolve_guide(slug, published_only=True)
        if guide is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        chapter = get_object_or_404(GuideChapter, guide=guide, slug=chapter_slug)
        return Response(GuideChapterSerializer(chapter).data)


class GuideViewCountView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, slug):
        guide, _ = _resolve_guide(slug, published_only=True)
        if guide is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        CareerGuide.objects.filter(pk=guide.pk).update(view_count=F("view_count") + 1)
        return Response({"ok": True})


def _progress_learner(request):
    # One account may own several learner profiles. Follow the active profile
    # selected in the shared login session, just like counselling intake.
    from .views import _resolve_learner_profile
    return _resolve_learner_profile(request)


def _progress_data(progress):
    chapter = progress.last_chapter
    return {
        "guide_slug": progress.guide.slug,
        "guide_title": progress.guide.title,
        "last_chapter_slug": chapter.slug if chapter else None,
        "last_chapter_title": chapter.title if chapter else None,
        "read_count": progress.read_chapters.count(),
        "read_chapter_slugs": [row.slug for row in progress.read_chapters.all()],
        "chapter_count": progress.guide.chapters.count(),
        "has_read": progress.has_read,
        "updated_at": progress.updated_at,
    }


class GuideProgressListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = _progress_learner(request)
        if learner is None:
            return Response({"detail": "Select a learner profile."}, status=400)
        rows = GuideReadingProgress.objects.filter(
            learner_profile=learner, guide__in=CareerGuide.objects.published(),
        ).select_related("guide", "last_chapter").prefetch_related("read_chapters", "guide__chapters")
        return Response({"results": [_progress_data(row) for row in rows]})


class GuideProgressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _context(self, request, slug):
        learner = _progress_learner(request)
        if learner is None:
            return None, None
        guide, _ = _resolve_guide(slug, published_only=True)
        return learner, guide

    def get(self, request, slug):
        learner, guide = self._context(request, slug)
        if learner is None:
            return Response({"detail": "Select a learner profile."}, status=400)
        if guide is None:
            return Response({"detail": "Guide not found."}, status=404)
        progress = GuideReadingProgress.objects.filter(
            learner_profile=learner, guide=guide,
        ).select_related("guide", "last_chapter").first()
        return Response(_progress_data(progress) if progress else {
            "guide_slug": guide.slug, "guide_title": guide.title,
            "last_chapter_slug": None, "last_chapter_title": None,
            "read_count": 0, "read_chapter_slugs": [],
            "chapter_count": guide.chapters.count(),
            "has_read": False, "updated_at": None,
        })

    def put(self, request, slug):
        learner, guide = self._context(request, slug)
        if learner is None:
            return Response({"detail": "Select a learner profile."}, status=400)
        if guide is None:
            return Response({"detail": "Guide not found."}, status=404)
        chapter_slug = request.data.get("chapter_slug")
        chapter = None
        if chapter_slug:
            chapter = guide.chapters.filter(slug=chapter_slug).first()
            if chapter is None:
                return Response({"detail": "Chapter not found in this guide."}, status=400)
        elif guide.chapters.exists():
            return Response({"detail": "chapter_slug is required."}, status=400)
        if request.data.get("mark_read") not in (None, True, False):
            return Response({"detail": "mark_read must be a boolean."}, status=400)

        progress, _ = GuideReadingProgress.objects.get_or_create(
            learner_profile=learner, guide=guide,
        )
        if chapter is not None:
            progress.last_chapter = chapter
        if request.data.get("mark_read") is True:
            if chapter is not None:
                progress.read_chapters.add(chapter)
                progress.has_read = progress.read_chapters.count() >= guide.chapters.count()
            else:
                progress.has_read = True
        progress.save()
        return Response(_progress_data(progress))


# =====================================================
# Staff CRUD
# =====================================================

class AdminGuideListView(APIView):
    permission_classes = [IsAuthenticated, IsContentEditor]

    def get(self, request):
        qs = CareerGuide.objects.all().order_by("order", "title")
        return Response(CareerGuideAdminSerializer(qs, many=True).data)

    def post(self, request):
        serializer = CareerGuideAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminGuideDetailView(APIView):
    permission_classes = [IsAuthenticated, IsContentEditor]

    def get(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        return Response(CareerGuideAdminSerializer(guide).data)

    def patch(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        serializer = CareerGuideAdminSerializer(guide, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        guide.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminGuideSectionListView(APIView):
    permission_classes = [IsAuthenticated, IsContentEditor]

    def get(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        return Response(GuideSectionSerializer(guide.sections.all(), many=True).data)

    def post(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        serializer = GuideSectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(guide=guide)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminSectionDetailView(APIView):
    permission_classes = [IsAuthenticated, IsContentEditor]

    def patch(self, request, pk):
        section = get_object_or_404(GuideSection, pk=pk)
        serializer = GuideSectionSerializer(section, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        section = get_object_or_404(GuideSection, pk=pk)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminGuideReorderView(APIView):
    """POST {"section_ids": [id, id, ...]} — sets .order to each id's
    position in the list. Only touches this guide's own sections."""

    permission_classes = [IsAuthenticated, IsContentEditor]

    def post(self, request, pk):
        guide = get_object_or_404(CareerGuide, pk=pk)
        ids = request.data.get("section_ids") or []
        owned = set(guide.sections.values_list("id", flat=True))
        updates = []
        for order, section_id in enumerate(ids):
            if section_id in owned:
                updates.append(GuideSection(id=section_id, order=order))
        GuideSection.objects.bulk_update(updates, ["order"])
        return Response({"reordered": len(updates)})


class AdminGuidePublishView(APIView):
    permission_classes = [IsAuthenticated, IsContentEditor]

    def post(self, request, pk):
        from content.models import PublishStatus

        guide = get_object_or_404(CareerGuide, pk=pk)
        target_status = request.data.get("status", PublishStatus.PUBLISHED)
        if target_status not in PublishStatus.values:
            return Response({"detail": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
        guide.status = target_status
        if request.data.get("publish_at"):
            guide.publish_at = request.data["publish_at"]
        guide.save(update_fields=["status", "publish_at", "updated_at"])
        return Response(CareerGuideAdminSerializer(guide).data)
