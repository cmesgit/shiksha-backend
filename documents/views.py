# Explore document-library public API. Mounted under /api/explore/.
#
# Mirrors the forum app's conventions: APIView classes, AllowAny reads /
# IsAuthenticated writes, manual {results, count} pagination via _int_param, a
# single annotated base queryset (_base_documents) that hides moderator-removed
# docs, a lazy ban/suspend gate (_ban_error), report dedup + self-report block,
# and ScopedRateThrottle on the write-heavy endpoints. Endpoint set matches the
# frontend's exploreApi.js header.

import logging
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework import status
from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Exists, OuterRef, Sum, F
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from .models import (
    Document, DocumentCategory, Collection, Follow, Report,
    SavedDocument, DocumentLike, DocumentProfile, DocTag,
    compute_file_hash,
)
from .serializers import (
    DocumentCardSerializer, DocumentDetailSerializer, DocumentCategorySerializer,
    CollectionSerializer, CollectionWriteSerializer, AddCollectionDocumentSerializer,
    CreateReportSerializer, UploadDocumentSerializer,
)
from .utils import contributor_badge
from . import constants

User = get_user_model()
logger = logging.getLogger(__name__)


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


def _verification_visible_q(user):
    """What community verification hides from the public library.

    Two different rules, and conflating them is the trap:

      * HIDDEN — two specialists objected to the same version. Out of Browse
        for everyone but its owner and an admin, always.
      * UNVERIFIED / CHANGES_REQUESTED — visible by default, because the
        design is explicit that a set with changes requested "stays in Browse,
        marked Unverified". Only the admin's "Show unverified uploads in
        Browse" switch takes them out, and turning it off converts the whole
        feature from a badge into a gate.

    In both cases the OWNER still sees their own set. A member whose upload
    silently vanished from the library with no explanation has no way to find
    the reviewer's note telling them how to fix it.
    """
    from global_settings.models import GlobalSettings
    from .models import VerificationSettings, VerificationStatus

    if not GlobalSettings.load().explore_verification_enabled:
        return None

    hidden = Q(verification_status=VerificationStatus.HIDDEN)
    if not VerificationSettings.load().show_unverified_in_browse:
        hidden = hidden | Q(verification_status__in=[
            VerificationStatus.UNVERIFIED, VerificationStatus.CHANGES_REQUESTED,
        ])

    blocked = Q(**{}) & hidden
    if user is not None and getattr(user, "is_authenticated", False):
        blocked = hidden & ~Q(owner=user)
    return ~blocked


def _base_documents(user, include_removed=False):
    """The single annotated base queryset for public listing/detail. Hides
    moderator-removed documents unless include_removed=True (moderator views)."""
    qs = Document.objects.select_related("owner", "owner__document_profile", "category", "research_submission").prefetch_related("research_submission__cases", "topics", "files")
    if not include_removed:
        qs = qs.filter(is_removed=False)
        visible = _verification_visible_q(user)
        if visible is not None:
            qs = qs.filter(visible)
    qs = qs.annotate(likes_count_annotated=Count("likes", distinct=True))
    if user and user.is_authenticated:
        qs = qs.annotate(
            is_saved_annotated=Exists(
                SavedDocument.objects.filter(document=OuterRef("pk"), user=user)),
            is_liked_annotated=Exists(
                DocumentLike.objects.filter(document=OuterRef("pk"), user=user)),
        )
    return qs


def _field_tree(user):
    """`field › subject › branch` with a document count on every node.

    ⚠ Counts are of DISTINCT documents, not of tag rows. A set tagged under
    two branches of the same subject would otherwise be counted twice in that
    subject — and the design puts these numbers on chips people use to decide
    whether a branch is worth opening.

    The static tree from `fields.field_groups()` is unioned in, so a subject
    with nothing in it still renders with a zero rather than vanishing. The
    design shows counts "for every chip including zero".
    """
    from collections import defaultdict

    from .fields import field_groups
    from .models import DocumentTopic

    visible = _base_documents(user).values("pk")
    rows = DocumentTopic.objects.filter(
        document__in=visible,
    ).values_list("field", "subject", "branch", "document_id")

    triples = []
    field_docs = defaultdict(set)
    subject_docs = defaultdict(set)
    branch_docs = defaultdict(set)
    for field, subject, branch, doc_id in rows:
        triples.append((field, subject, branch))
        field_docs[field].add(doc_id)
        subject_docs[(field, subject)].add(doc_id)
        if branch:
            branch_docs[(field, subject, branch)].add(doc_id)

    tree = field_groups(triples)
    out = []
    for group in tree:
        field = group["field"]
        subjects = []
        for entry in group["subjects"]:
            subject = entry["subject"]
            subjects.append({
                "subject": subject,
                "count": len(subject_docs.get((field, subject), ())),
                "branches": [
                    {"branch": b,
                     "count": len(branch_docs.get((field, subject, b), ()))}
                    for b in entry["branches"]
                ],
            })
        out.append({
            "field": field,
            "count": len(field_docs.get(field, ())),
            "subjects": subjects,
        })
    return out


def _attach_set_children(doc, data, first_upload):
    """Write the extra files and the extra tag paths of a set.

    The FIRST file and the FIRST tag are already on the parent, and
    `documents/set_signals.py` has turned each into a child row by the time
    this runs. So this adds only what the parent cannot hold — which is why
    it skips index 0 of neither list but instead relies on the signal having
    claimed the parent's copy, and de-duplicates by hash and by path.
    """
    from .fields import field_for_subject
    from .models import DocumentFile, DocumentTopic, compute_file_hash

    extra_files = data.get("files") or []
    seen_hashes = set(
        doc.files.exclude(file_hash="").values_list("file_hash", flat=True))
    order = doc.files.count()
    for upload in extra_files:
        digest = compute_file_hash(upload)
        # The same bytes twice in one set is a slip, not an upload. Skipping
        # is kinder than a 409 that discards the other nineteen files.
        if digest and digest in seen_hashes:
            continue
        if first_upload is not None and digest and digest == (doc.file_hash or ""):
            continue
        DocumentFile.objects.create(
            document=doc, owner_id=doc.owner_id, file=upload,
            original_name=getattr(upload, "name", "") or "",
            filetype=(getattr(upload, "name", "").rsplit(".", 1)[-1] or "").upper()[:10],
            size=getattr(upload, "size", 0) or 0,
            file_hash=digest, order=order,
        )
        seen_hashes.add(digest)
        order += 1

    # "Field|Subject|Branch" per entry. Subject is the only required part —
    # field is derivable and branch is optional, exactly as on the parent.
    existing = {
        (t.subject.casefold(), t.branch.casefold())
        for t in doc.topics.all()
    }
    position = doc.topics.count()
    for raw in (data.get("topics") or []):
        parts = [p.strip() for p in str(raw).split("|")]
        field = parts[0] if len(parts) > 2 else ""
        subject = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        branch = parts[-1] if len(parts) > 1 else ""
        if not subject:
            continue
        key = (subject.casefold(), branch.casefold())
        if key in existing:
            continue
        DocumentTopic.objects.create(
            document=doc, field=field or field_for_subject(subject),
            subject=subject, branch=branch, order=position,
        )
        existing.add(key)
        position += 1


def _filter_by_verification(qs, status_param, user):
    """`?status=verified|unverified|needs_my_review`."""
    from global_settings.models import GlobalSettings
    from .models import VerificationStatus
    from . import verification as v

    value = (status_param or "").strip().lower()
    if not value or value in ("all", "any"):
        return qs
    if not GlobalSettings.load().explore_verification_enabled:
        return qs

    if value == "verified":
        return qs.filter(verification_status=VerificationStatus.VERIFIED)
    if value == "unverified":
        # "Unverified" to a reader means "not yet confirmed", which includes a
        # set whose confirmations are paused — the public badge on both says
        # Unverified, so the filter has to agree with the badge.
        return qs.filter(verification_status__in=[
            VerificationStatus.UNVERIFIED, VerificationStatus.CHANGES_REQUESTED,
        ])
    if value == "needs_my_review":
        if not getattr(user, "is_authenticated", False):
            return qs.none()
        # Reuses the reviewer's own queue rather than re-deriving "sets in my
        # subjects I have not reviewed" — which is the same rule, and two
        # copies of it would drift the moment a setting changed.
        return qs.filter(pk__in=v.review_queue(user).values("pk"))
    return qs


def _sort_documents(qs, sort):
    if sort == "Latest":
        return qs.order_by("-created_at", "-pk")
    if sort == "Most Viewed":
        return qs.order_by("-view_count", "-created_at", "-pk")
    if sort == "Most Downloaded":
        return qs.order_by("-download_count", "-created_at", "-pk")
    # Trending (default): featured/trending first, then views.
    return qs.order_by("-is_trending", "-view_count", "-created_at", "-pk")


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


def _owned_collection(request, slug):
    """Fetch a collection by slug and verify the requesting user is its
    curator. Returns (collection, None) on success or (None, Response) with
    the appropriate 401/403 to return as-is."""
    col = get_object_or_404(Collection, slug=slug)
    if not request.user.is_authenticated:
        return None, Response({"detail": "Authentication required."},
                              status=status.HTTP_401_UNAUTHORIZED)
    if col.curator_id != request.user.id:
        return None, Response({"detail": "Only the collection owner can do this."},
                              status=status.HTTP_403_FORBIDDEN)
    return col, None


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
        from .taxonomy import subject_groups
        cats = DocumentCategory.objects.filter(is_active=True)
        pairs = Document.objects.filter(is_removed=False).order_by("subject", "topic").values_list("subject", "topic").distinct()
        groups = subject_groups(pairs)
        return Response({
            "categories": DocumentCategorySerializer(
                cats, many=True, context={"request": request}).data,
            "subjects": ["All", *(group["subject"] for group in groups)],
            "subjectGroups": groups,
            # The three-level tree, with a count on every node. Served from
            # the same endpoint as the two-level one rather than a new route,
            # because both the browse panel and the upload tag picker need it
            # and they already fetch this.
            "fieldGroups": _field_tree(request.user),
            "levels": constants.LEVELS,
            "languages": constants.LANGUAGES,
            "filetypes": constants.FILETYPES,
            "fileKinds": constants.FILE_KINDS,
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
        # Note: presence of the param (even as an empty string, e.g. a user
        # with nothing saved) must short-circuit to an explicit result —
        # falling through to the general listing below would show
        # everything instead of nothing.
        if "ids" in request.query_params:
            ids_param = (p.get("ids") or "").strip()
            ids = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
            rows = qs.filter(pk__in=ids)
            return Response({
                "results": DocumentCardSerializer(rows, many=True, context={"request": request}).data,
                "count": rows.count(),
            })

        # `?mine=1` — the signed-in user's own uploads. My Uploads used to be
        # driven off a localStorage id list written at publish time, so it was
        # empty on any other browser or after a cache clear, and a document
        # uploaded from another device never appeared at all. This is the real
        # answer to "what have I published".
        if p.get("mine") in ("1", "true", "yes"):
            if not request.user.is_authenticated:
                return Response({"detail": "Authentication required."},
                                status=status.HTTP_401_UNAUTHORIZED)
            rows = qs.filter(owner=request.user).order_by("-created_at")
            return Response({
                "results": DocumentCardSerializer(rows, many=True, context={"request": request}).data,
                "count": rows.count(),
            })

        category = p.get("category")
        if category and category != "All":
            qs = qs.filter(category__slug=category)
        for field in ("subject", "topic", "level", "language", "filetype"):
            val = p.get(field)
            if val and val != "All":
                qs = qs.filter(**{f"{field}__iexact": val.strip()})

        # The three-level browse panel. These read DocumentTopic rather than
        # the flat columns above, because a set carries SEVERAL paths and
        # matching any one of them should find it.
        tfield = (p.get("field") or "").strip()
        if tfield and tfield != "All":
            qs = qs.filter(topics__field__iexact=tfield).distinct()
        tsubject = (p.get("tsubject") or "").strip()
        if tsubject and tsubject != "All":
            qs = qs.filter(topics__subject__iexact=tsubject).distinct()
        tbranch = (p.get("branch") or "").strip()
        if tbranch and tbranch != "All":
            qs = qs.filter(topics__branch__iexact=tbranch).distinct()

        # The design's file-type row is families (Documents, Slides, Data,
        # Images, Audio and video, Other), not extensions. It reads the set's
        # FILES, so a set with a PDF and an XLSX answers to both.
        kind = (p.get("filekind") or "").strip().lower()
        if kind and kind not in ("all", "all files"):
            exts = constants.FILE_KIND_EXTS.get(kind)
            if exts:
                if kind == "other":
                    known = {e for group in constants.FILE_KIND_EXTS.values()
                             for e in group if group is not exts}
                    qs = qs.exclude(files__filetype__in=[e.upper() for e in known]).distinct()
                else:
                    qs = qs.filter(
                        files__filetype__in=[e.upper() for e in exts]).distinct()
        # The design's status filter row: Any status / Verified / Unverified /
        # Needs my review. Silently ignored while the feature is off, so an
        # old bookmarked URL does not 400.
        qs = _filter_by_verification(qs, p.get("status"), request.user)

        cutoff = _within_range_cutoff(p.get("date"))
        if cutoff is not None:
            qs = qs.filter(created_at__gte=cutoff)
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(description__icontains=q)
                | Q(subject__icontains=q) | Q(topic__icontains=q) | Q(tags__name__icontains=q)
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

        if slug == "research-papers":
            return Response({"detail": "Research papers need review before publication. Submit through Research Hub.",
                             "submit_path": "/explore/research"}, status=400)

        # Refuse a re-upload of the same bytes by the same person. Checked here
        # rather than relying on the DB constraint alone so the uploader gets a
        # readable message naming the document they already have, instead of a
        # 500 out of IntegrityError. The constraint still backstops a race.
        upload = data.get("file")
        file_hash = compute_file_hash(upload)
        if file_hash:
            existing = Document.objects.filter(
                owner=request.user, file_hash=file_hash, is_removed=False
            ).first()
            if existing is not None:
                return Response(
                    {
                        "detail": (
                            f'You have already uploaded this file as "{existing.title}". '
                            f"Delete that document first if you want to replace it."
                        ),
                        "duplicate_of": existing.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        doc = Document.objects.create(
            owner=request.user,
            title=data["title"],
            description=data.get("description", ""),
            full=data.get("full", ""),
            category=category,
            subject=data.get("subject", ""),
            topic=data.get("topic", ""),
            level=data.get("level", ""),
            language=data.get("language", "English"),
            institution=data.get("institution", ""),
            filetype=data.get("filetype", "PDF"),
            pages=data.get("pages", 0),
            file=upload,
            file_hash=file_hash,
        )
        for name in [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]:
            tag, _ = DocTag.objects.get_or_create(name=name.lower())
            doc.tags.add(tag)

        _attach_set_children(doc, data, upload)

        # ⚠ Invite AFTER the tag rows exist. Matching reads DocumentTopic, and
        # a post_save on Document would run before _attach_set_children and
        # find a set with no subjects — i.e. find nobody, every time.
        from .verification_signals import invite_for
        invite_for(doc)

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

    def delete(self, request, document_id):
        """Let an uploader take their own document down. Staff may also.

        Soft-remove rather than a row delete, for the same reason moderation
        does: `Report` reaches a document through a GenericForeignKey with a
        plain `object_id` and no cascade, so a hard delete would strand open
        reports in the moderation queue pointing at nothing.

        The stored file IS deleted, though. The row surviving is a bookkeeping
        detail; the bytes surviving is not — Explore media sits in a public
        Bunny zone, so "I deleted it" has to mean the file stops being
        retrievable, not just that it stops being listed.

        BunnyStorage.delete() removes the object from Edge Storage *and* purges
        it from the CDN edge — deleting from storage alone leaves the pull zone
        serving its cached copy indefinitely (measured: origin 404, CDN still
        200 a minute later). The purge needs BUNNY_ACCOUNT_API_KEY. When that
        is unset the delete still happens but the file stays downloadable by
        anyone holding the URL, so the response says `file_purged: false`
        instead of reporting a clean removal.
        """
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."},
                            status=status.HTTP_401_UNAUTHORIZED)
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        if doc.owner_id != request.user.id and not request.user.is_staff:
            return Response({"detail": "You can only delete your own uploads."},
                            status=status.HTTP_403_FORBIDDEN)

        if getattr(doc, "research_submission", None) is not None:
            return Response({"detail": "Reviewed research papers must be withdrawn through Research Hub. Contact an administrator.",
                             "research_path": "/explore/research"}, status=403)

        file_removed = True
        if doc.file:
            storage = doc.file.storage
            # Never let a storage-backend hiccup strand the document as still
            # visible — take it out of the library either way.
            try:
                doc.file.delete(save=False)   # origin delete, then CDN purge
            except Exception:
                logger.exception("Could not delete stored file for document %s", doc.pk)
                file_removed = False
            else:
                # Deliberately reports whether purging is CONFIGURED, not the
                # result of this particular purge call: the storage object is
                # shared across concurrent requests, so per-call state stashed
                # on it would be read by the wrong request. A missing account
                # key is the real, actionable condition anyway — and it is a
                # constant, so this answer is never wrong for the wrong reason.
                file_removed = not (
                    hasattr(storage, "purge") and not getattr(settings, "BUNNY_ACCOUNT_API_KEY", "")
                )
        doc.file = None
        # Frees the (owner, file_hash) slot so the same file can be uploaded
        # again later; the constraint already excludes removed rows, but
        # clearing it keeps the column honest about what bytes still exist.
        doc.file_hash = ""
        doc.is_removed = True
        doc.removed_at = timezone.now()
        doc.save(update_fields=["file", "file_hash", "is_removed", "removed_at"])
        return Response({"deleted": True, "file_purged": file_removed},
                        status=status.HTTP_200_OK)


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
        # F() rather than read-then-write: two readers opening the same
        # document at once both computed `view_count + 1` from the same stale
        # read and one increment was lost.
        Document.objects.filter(pk=doc.pk).update(view_count=F("view_count") + 1)
        doc.refresh_from_db(fields=["view_count"])
        return Response({"views": doc.view_count})


class RecordDownloadView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        Document.objects.filter(pk=doc.pk).update(download_count=F("download_count") + 1)
        doc.refresh_from_db(fields=["download_count"])
        return Response({"downloads": doc.download_count})


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
        # Only the curator (or staff) sees their private collections here.
        # Without this filter the endpoint — which is AllowAny — listed every
        # collection the author owned, private ones included, to anyone. That
        # also made the count on a profile disagree with the owner's own My
        # Library, which is how the discrepancy was first noticed.
        collections = Collection.objects.filter(curator=user)
        viewer_is_owner = request.user.is_authenticated and (
            request.user.id == user.id or request.user.is_staff
        )
        if not viewer_is_owner:
            collections = collections.filter(visibility=Collection.VIS_PUBLIC)
        collections = collections.annotate(
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
        # The public catalogue and the owner's library have different scopes.
        # Never include private collections in the public list, even for staff.
        if request.query_params.get("mine") == "1":
            if not request.user.is_authenticated:
                return Response({"detail": "Authentication required."},
                                status=status.HTTP_401_UNAUTHORIZED)
            cols = Collection.objects.filter(curator=request.user)
        else:
            cols = Collection.objects.filter(visibility=Collection.VIS_PUBLIC)
        cols = cols.annotate(
            doc_count_annotated=Count("documents", filter=Q(documents__is_removed=False), distinct=True))
        return Response(
            CollectionSerializer(cols, many=True, context={"request": request}).data)

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."},
                            status=status.HTTP_401_UNAUTHORIZED)
        banned = _ban_error(request.user)
        if banned is not None:
            return banned

        serializer = CollectionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        col = Collection.objects.create(
            curator=request.user,
            title=data["title"],
            description=data.get("description", ""),
            color=data.get("color", "#125027"),
            visibility=data.get("visibility", Collection.VIS_PUBLIC),
        )
        return Response(
            CollectionSerializer(col, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class CollectionDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        col = get_object_or_404(Collection, slug=slug)
        # A PRIVATE collection is only for its curator (and staff).
        #
        # This checked nothing, while CollectionsView.get right above it
        # deliberately lists only VIS_PUBLIC — so private was a real, intended
        # state that this endpoint ignored. Slugs are derived from the title
        # and therefore guessable, and the view is AllowAny, so a private
        # collection's title, description and full document list were readable
        # with no login at all. patch/delete on this same class already go
        # through _owned_collection; only get was open.
        if col.visibility != Collection.VIS_PUBLIC:
            user = request.user
            is_owner = user.is_authenticated and col.curator_id == user.id
            if not (is_owner or (user.is_authenticated and user.is_staff)):
                raise Http404()
        ctx = {"request": request}
        docs = col.documents.filter(is_removed=False).select_related(
            "owner", "owner__document_profile", "category", "research_submission").prefetch_related("research_submission__cases")
        data = CollectionSerializer(col, context=ctx).data
        data["docs"] = DocumentCardSerializer(docs, many=True, context=ctx).data
        return Response(data)

    def patch(self, request, slug):
        col, err = _owned_collection(request, slug)
        if err is not None:
            return err
        serializer = CollectionWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field in ("title", "description", "color", "visibility"):
            if field in serializer.validated_data:
                setattr(col, field, serializer.validated_data[field])
        col.save()
        return Response(CollectionSerializer(col, context={"request": request}).data)

    def delete(self, request, slug):
        col, err = _owned_collection(request, slug)
        if err is not None:
            return err
        col.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CollectionDocumentsView(APIView):
    """Add a document to the requesting user's own collection."""
    permission_classes = [IsAuthenticated]

    def post(self, request, slug):
        col, err = _owned_collection(request, slug)
        if err is not None:
            return err
        banned = _ban_error(request.user)
        if banned is not None:
            return banned
        serializer = AddCollectionDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = get_object_or_404(
            Document, pk=serializer.validated_data["document_id"], is_removed=False)
        col.documents.add(doc)
        return Response(
            CollectionSerializer(col, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class CollectionDocumentDetailView(APIView):
    """Remove a document from the requesting user's own collection."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, slug, document_id):
        col, err = _owned_collection(request, slug)
        if err is not None:
            return err
        doc = get_object_or_404(Document, pk=document_id)
        col.documents.remove(doc)
        return Response(status=status.HTTP_204_NO_CONTENT)


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
