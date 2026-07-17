# Explore document-library public API. Mounted under /api/explore/.
#
# Mirrors the forum app's conventions: APIView classes, AllowAny reads /
# IsAuthenticated writes, manual {results, count} pagination via _int_param, a
# single annotated base queryset (_base_documents) that hides moderator-removed
# docs, a lazy ban/suspend gate (_ban_error), report dedup + self-report block,
# and ScopedRateThrottle on the write-heavy endpoints. Endpoint set matches the
# frontend's exploreApi.js header.

from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Exists, OuterRef, Sum
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from .models import (
    Document, DocumentCategory, Collection, Follow, Report,
    SavedDocument, DocumentLike, DocumentProfile, DocTag,
)
from .serializers import (
    DocumentCardSerializer, DocumentDetailSerializer, DocumentCategorySerializer,
    CollectionSerializer, CreateReportSerializer, UploadDocumentSerializer,
)
from .utils import contributor_badge
from . import constants

User = get_user_model()


# =====================================================
# Helpers
# =====================================================
def _int_param(request, name, default, maximum):
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(1, value), maximum)


def _ban_error(user):
    """A banned or currently-suspended uploader cannot write. Suspension lifts
    itself lazily here (no cron). Returns a Response or None."""
    profile, _ = DocumentProfile.objects.get_or_create(user=user)
    if profile.is_banned:
        return Response(
            {"detail": "You have been banned from the Explore library."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if profile.suspended_until and profile.suspended_until > timezone.now():
        return Response(
            {"detail": f"Your upload access is suspended until {profile.suspended_until:%d %b %Y}."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _base_documents(user, include_removed=False):
    """The single annotated base queryset for public listing/detail. Hides
    moderator-removed documents unless include_removed=True (moderator views)."""
    qs = Document.objects.select_related("owner", "owner__document_profile", "category")
    if not include_removed:
        qs = qs.filter(is_removed=False)
    if user and user.is_authenticated:
        qs = qs.annotate(
            is_saved_annotated=Exists(
                SavedDocument.objects.filter(document=OuterRef("pk"), user=user)),
            is_liked_annotated=Exists(
                DocumentLike.objects.filter(document=OuterRef("pk"), user=user)),
        )
    return qs


def _sort_documents(qs, sort):
    if sort == "Latest":
        return qs.order_by("-created_at")
    if sort == "Most Viewed":
        return qs.order_by("-view_count", "-created_at")
    if sort == "Most Downloaded":
        return qs.order_by("-download_count", "-created_at")
    # Trending (default): featured/trending first, then views.
    return qs.order_by("-is_trending", "-view_count", "-created_at")


def _within_range_cutoff(range_label):
    """Return a datetime cutoff for a DATE_RANGES label, or None for 'Any time'."""
    now = timezone.now()
    spans = {
        "Past 24 hours": timedelta(days=1),
        "Past week": timedelta(days=7),
        "Past month": timedelta(days=30),
        "Past year": timedelta(days=365),
    }
    span = spans.get(range_label)
    return (now - span) if span else None


def _toggle_follow(user, target_type, target_key):
    obj, created = Follow.objects.get_or_create(
        user=user, target_type=target_type, target_key=str(target_key))
    if not created:
        obj.delete()
        return False
    return True


# =====================================================
# Facets (filter options)
# =====================================================
class FacetsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        cats = DocumentCategory.objects.filter(is_active=True)
        return Response({
            "categories": DocumentCategorySerializer(
                cats, many=True, context={"request": request}).data,
            "subjects": constants.SUBJECTS,
            "levels": constants.LEVELS,
            "languages": constants.LANGUAGES,
            "filetypes": constants.FILETYPES,
            "sorts": constants.SORTS,
            "dateRanges": constants.DATE_RANGES,
            "uploadTypes": constants.UPLOAD_TYPES,
        })


# =====================================================
# Landing rails
# =====================================================
class LandingView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        ctx = {"request": request}
        cats = DocumentCategory.objects.filter(is_active=True).annotate(
            doc_count_annotated=Count("documents", filter=Q(documents__is_removed=False), distinct=True))
        base = _base_documents(request.user)

        featured = base.filter(is_featured=True)[:4]
        trending = _sort_documents(base.filter(is_trending=True), "Trending")[:10]
        recent = base.order_by("-created_at")[:4]

        # Top contributors: owners ranked by (non-removed) document count.
        top_owner_ids = list(
            Document.objects.filter(is_removed=False)
            .values("owner").annotate(n=Count("id")).order_by("-n")
            .values_list("owner", flat=True)[:6]
        )
        owners = User.objects.filter(id__in=top_owner_ids).select_related("document_profile")
        authors = [_author_blob(u) for u in owners]

        collections = Collection.objects.filter(visibility=Collection.VIS_PUBLIC).annotate(
            doc_count_annotated=Count("documents", filter=Q(documents__is_removed=False), distinct=True)
        )[:6]

        return Response({
            "categories": DocumentCategorySerializer(cats, many=True, context=ctx).data,
            "trendChips": constants.TREND_CHIPS,
            "featured": DocumentCardSerializer(featured, many=True, context=ctx).data,
            "trending": DocumentCardSerializer(trending, many=True, context=ctx).data,
            "recent": DocumentCardSerializer(recent, many=True, context=ctx).data,
            "authors": authors,
            "collections": CollectionSerializer(collections, many=True, context=ctx).data,
        })


def _author_blob(user):
    """Contributor card blob (badge + follower / doc / view / download counts)."""
    badge = contributor_badge(user)
    docs = Document.objects.filter(owner=user, is_removed=False)
    agg = docs.aggregate(
        docs=Count("id"), views=Sum("view_count"), downloads=Sum("download_count"))
    followers = Follow.objects.filter(
        target_type=Follow.TARGET_AUTHOR, target_key=user.username).count()
    profile = getattr(user, "document_profile", None)
    return {
        **badge,
        "bio": (getattr(profile, "bio", "") or "") if profile else "",
        "followers": followers,
        "docsCount": agg["docs"] or 0,
        "views": agg["views"] or 0,
        "downloads": agg["downloads"] or 0,
    }


# =====================================================
# Documents — search (GET) + upload (POST)
# =====================================================
class DocumentsView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_scope = "documents_upload"

    def get_throttles(self):
        # Only throttle the write (upload) path.
        if self.request.method == "POST":
            return [ScopedRateThrottle()]
        return []

    def get(self, request):
        qs = _base_documents(request.user)
        p = request.query_params

        # `?ids=1,2,3` — fetch a specific set (used by the Library page for
        # saved / history / following lists). Returns them unpaginated.
        ids_param = (p.get("ids") or "").strip()
        if ids_param:
            ids = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
            rows = qs.filter(pk__in=ids)
            return Response({
                "results": DocumentCardSerializer(rows, many=True, context={"request": request}).data,
                "count": rows.count(),
            })

        category = p.get("category")
        if category and category != "All":
            qs = qs.filter(category__slug=category)
        for field in ("subject", "level", "language", "filetype"):
            val = p.get(field)
            if val and val != "All":
                qs = qs.filter(**{field: val})
        cutoff = _within_range_cutoff(p.get("date"))
        if cutoff is not None:
            qs = qs.filter(created_at__gte=cutoff)
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(description__icontains=q)
                | Q(subject__icontains=q) | Q(tags__name__icontains=q)
                | Q(owner__username__icontains=q)
            ).distinct()

        qs = _sort_documents(qs, p.get("sort") or "Trending")

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 24, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = qs[start:start + page_size]
        return Response({
            "results": DocumentCardSerializer(rows, many=True, context={"request": request}).data,
            "count": total,
        })

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."},
                            status=status.HTTP_401_UNAUTHORIZED)
        banned = _ban_error(request.user)
        if banned is not None:
            return banned

        serializer = UploadDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        category = None
        slug = (data.get("category") or "").strip()
        if slug:
            category = DocumentCategory.objects.filter(slug=slug, is_active=True).first()

        doc = Document.objects.create(
            owner=request.user,
            title=data["title"],
            description=data.get("description", ""),
            full=data.get("full", ""),
            category=category,
            subject=data.get("subject", ""),
            level=data.get("level", ""),
            language=data.get("language", "English"),
            institution=data.get("institution", ""),
            filetype=data.get("filetype", "PDF"),
            pages=data.get("pages", 0),
            file=data.get("file"),
        )
        for name in [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]:
            tag, _ = DocTag.objects.get_or_create(name=name.lower())
            doc.tags.add(tag)

        return Response(
            DocumentDetailSerializer(doc, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class DocumentDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, document_id):
        doc = get_object_or_404(_base_documents(request.user), pk=document_id)
        ctx = {"request": request}
        related = _base_documents(request.user).filter(
            Q(category=doc.category) | Q(subject=doc.subject)
        ).exclude(pk=doc.id)[:4]
        recommended = _base_documents(request.user).exclude(
            owner=doc.owner).exclude(pk=doc.id)[:6]
        return Response({
            "doc": DocumentDetailSerializer(doc, context=ctx).data,
            "related": DocumentCardSerializer(related, many=True, context=ctx).data,
            "recommended": DocumentCardSerializer(recommended, many=True, context=ctx).data,
        })


# =====================================================
# Document write actions
# =====================================================
class ToggleSaveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        existing = SavedDocument.objects.filter(user=request.user, document=doc).first()
        if existing:
            existing.delete()
            return Response({"saved": False})
        SavedDocument.objects.create(user=request.user, document=doc)
        return Response({"saved": True})


class ToggleLikeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        existing = DocumentLike.objects.filter(user=request.user, document=doc).first()
        if existing:
            existing.delete()
            return Response({"liked": False, "likes": doc.likes.count()})
        DocumentLike.objects.create(user=request.user, document=doc)
        return Response({"liked": True, "likes": doc.likes.count()})


class RecordViewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        Document.objects.filter(pk=doc.pk).update(view_count=doc.view_count + 1)
        return Response({"views": doc.view_count + 1})


class RecordDownloadView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        Document.objects.filter(pk=doc.pk).update(download_count=doc.download_count + 1)
        return Response({"downloads": doc.download_count + 1})


class CreateReportView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "documents_report"

    def post(self, request, document_id):
        banned = _ban_error(request.user)
        if banned is not None:
            return banned

        serializer = CreateReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = get_object_or_404(Document, pk=document_id)
        ct = ContentType.objects.get_for_model(Document)

        # Can't report your own upload, and can't file a second open report.
        if doc.owner_id == request.user.id:
            return Response({"detail": "You can't report your own document."},
                            status=status.HTTP_400_BAD_REQUEST)
        if Report.objects.filter(
            reporter=request.user, content_type=ct, object_id=doc.id, resolved=False
        ).exists():
            return Response(
                {"detail": "You've already reported this — a moderator will review it."},
                status=status.HTTP_200_OK,
            )
        Report.objects.create(
            reporter=request.user, content_type=ct, object_id=doc.id,
            reason=serializer.validated_data["reason"],
            detail=serializer.validated_data.get("detail", ""),
        )
        return Response({"detail": "Reported to moderators — thanks for flagging."},
                        status=status.HTTP_201_CREATED)


# =====================================================
# Author (contributor)
# =====================================================
class AuthorDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, author_key):
        user = get_object_or_404(User, username=author_key)
        ctx = {"request": request}
        docs = _base_documents(request.user).filter(owner=user).order_by("-created_at")
        collections = Collection.objects.filter(curator=user).annotate(
            doc_count_annotated=Count("documents", filter=Q(documents__is_removed=False), distinct=True))
        author = _author_blob(user)
        if request.user.is_authenticated:
            author["is_following"] = Follow.objects.filter(
                user=request.user, target_type=Follow.TARGET_AUTHOR,
                target_key=user.username).exists()
        else:
            author["is_following"] = False
        return Response({
            "author": author,
            "docs": DocumentCardSerializer(docs, many=True, context=ctx).data,
            "collections": CollectionSerializer(collections, many=True, context=ctx).data,
        })


class FollowAuthorView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, author_key):
        user = get_object_or_404(User, username=author_key)
        following = _toggle_follow(request.user, Follow.TARGET_AUTHOR, user.username)
        return Response({"following": following})


# =====================================================
# Collections
# =====================================================
class CollectionsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        cols = Collection.objects.filter(visibility=Collection.VIS_PUBLIC).annotate(
            doc_count_annotated=Count("documents", filter=Q(documents__is_removed=False), distinct=True))
        return Response(
            CollectionSerializer(cols, many=True, context={"request": request}).data)


class CollectionDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        col = get_object_or_404(Collection, slug=slug)
        ctx = {"request": request}
        docs = col.documents.filter(is_removed=False).select_related(
            "owner", "owner__document_profile", "category")
        data = CollectionSerializer(col, context=ctx).data
        data["docs"] = DocumentCardSerializer(docs, many=True, context=ctx).data
        return Response(data)


# =====================================================
# Current user's Explore context (hydration)
# =====================================================
class DocumentsMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        profile, _ = DocumentProfile.objects.get_or_create(user=u)
        badge = contributor_badge(u)
        saved = list(SavedDocument.objects.filter(user=u).values_list("document_id", flat=True))
        liked = list(DocumentLike.objects.filter(user=u).values_list("document_id", flat=True))
        following = {"authors": [], "collections": [], "categories": []}
        for f in Follow.objects.filter(user=u):
            if f.target_type == Follow.TARGET_AUTHOR:
                following["authors"].append(f.target_key)
            elif f.target_type == Follow.TARGET_COLLECTION:
                following["collections"].append(f.target_key)
            elif f.target_type == Follow.TARGET_CATEGORY:
                following["categories"].append(f.target_key)
        perms = sorted(u.get_permissions())
        is_moderator = bool(
            u.is_staff or "documents.moderate" in perms or u.has_role("MODERATOR"))
        return Response({
            **badge,
            "headline": profile.headline or badge["title"],
            "institution": profile.institution,
            "bio": profile.bio,
            "saved": saved,
            "liked": liked,
            "following": following,
            "is_moderator": is_moderator,
            "permissions": perms,
        })
