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

import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from .admin_serializers import (
    AnnouncementAdminSerializer, BlogPostAdminSerializer,
    ContentImageAdminSerializer, ContentTagSerializer,
    CurrentAffairAdminSerializer, FAQItemAdminSerializer,
    HomeContentBlockAdminSerializer, HomeFloaterAdminSerializer,
    HomeListItemAdminSerializer, HomeSectionOrderAdminSerializer,
    ShowcaseCategoryAdminSerializer,
    ShowcaseCourseAdminSerializer,
)
from .models import (
    Announcement, BlogPost, BlogRevision, ContentImage, ContentRevision,
    ContentTag,
    CurrentAffair, FAQItem, HomeContentBlock, HomeFloater, HomeListItem,
    HomeSectionOrder, Locale, PublishStatus, ShowcaseCategory,
    ShowcaseCourse,
)
from .permissions import IsContentEditor
from .revisions import (
    record_deletion, record_revision, snapshot_before, snapshot_of,
)


class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50


class RecordsRevisions:
    """Write a ContentRevision for every change a person makes through the API.

    Mixed into the admin viewsets rather than wired to ``post_save`` — see
    ``ContentRevision``'s own docstring for why a signal is wrong here (it
    would fire during migrations and during ``seed_content``'s ~150-row run,
    filling the History screen with entries nobody caused).

    Until this existed, ``record_revision`` had exactly ONE production call
    site — the page-editor publish — so editing an FAQ, a notice, a card, an
    affair, a list item, a badge or a tag wrote no history at all, the History
    feed and the Studio home's recent-changes panel stayed empty, and four of
    the six declared actions were dead constants.

    A status change gets its own verb (published / hidden) instead of a flat
    "updated", because that is the distinction an editor scanning the feed
    actually cares about.

    Note for BlogPost: this coexists with ``BlogRevision`` and does not replace
    it. That one is body-only with deliberately unbounded retention and the
    block editor depends on its shape; this one is the cross-model feed.
    """

    def _revision_actor(self):
        user = getattr(self.request, "user", None)
        return user if (user is not None and user.is_authenticated) else None

    @staticmethod
    def _action_for(before, after):
        """`published` / `hidden` when the status moved, else `updated`."""
        was, now = (before or {}).get("status"), getattr(after, "status", None)
        if now == was:
            return ContentRevision.ACTION_UPDATED
        if now == PublishStatus.PUBLISHED:
            return ContentRevision.ACTION_PUBLISHED
        if now in (PublishStatus.DRAFT, PublishStatus.ARCHIVED):
            return ContentRevision.ACTION_HIDDEN
        return ContentRevision.ACTION_UPDATED

    def perform_create(self, serializer):
        super().perform_create(serializer)
        record_revision(
            serializer.instance,
            ContentRevision.ACTION_CREATED,
            actor=self._revision_actor(),
        )

    def perform_update(self, serializer):
        # Snapshot from a fresh query, not serializer.instance: FullCleanMixin
        # setattr()s the incoming payload onto that same object during
        # is_valid(), which DRF runs before this method, so the in-memory copy
        # already holds the NEW values. Same trap BlogPostAdminViewSet
        # documents at length for BlogRevision.
        before = snapshot_before(serializer.instance)
        super().perform_update(serializer)
        record_revision(
            serializer.instance,
            self._action_for(before, serializer.instance),
            actor=self._revision_actor(),
            snapshot=before,
        )

    def perform_destroy(self, instance):
        model, pk = instance.__class__, instance.pk
        before = snapshot_of(instance)
        super().perform_destroy(instance)
        record_deletion(model, pk, before, actor=self._revision_actor())

    def _record_status_action(self, obj, before):
        """For the publish/unpublish @actions, which set status and save()
        directly and so never reach perform_update."""
        record_revision(
            obj,
            self._action_for(before, obj),
            actor=self._revision_actor(),
            snapshot=before,
        )


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

class TagAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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

class FAQItemAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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

class AnnouncementAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination


# ── Showcase courses ──────────────────────────────────────────────

class ShowcaseCourseAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
    queryset = ShowcaseCourse.objects.all()
    serializer_class = ShowcaseCourseAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    # Explicit even though DRF's global default already includes these
    # (no DEFAULT_PARSER_CLASSES override in config/settings_base.py) —
    # `image` is a file upload field, so multipart support must not be
    # left to an implicit default that could change.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """POST {"cards": ["<id>", "<id>", ...]} — every card id in the desired
        order.

        Demands the COMPLETE set, like HomeSectionOrderAdminViewSet.reorder and
        for the same reason: a partial list from a stale tab would renumber a
        subset and silently reshuffle the homepage against the editor's
        intent. Until now the only way to order these cards was to type an
        integer into each one's edit form, one card at a time, while the grid
        rendered a drag handle that did nothing.
        """
        ids = request.data.get("cards")
        if not isinstance(ids, list) or not ids:
            return Response(
                {"detail": "cards must be a non-empty list of card ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # str() both sides: ids arrive as JSON strings, pks are UUIDs.
        given = [str(i) for i in ids]
        existing = {str(pk) for pk in ShowcaseCourse.objects.values_list("pk", flat=True)}
        if len(given) != len(set(given)) or set(given) != existing:
            return Response(
                {"detail": "cards must contain each existing card exactly once.",
                 "missing": sorted(existing - set(given)),
                 "unexpected": sorted(set(given) - existing)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            rows = {
                str(r.pk): r
                for r in ShowcaseCourse.objects.select_for_update()
            }
            for i, cid in enumerate(given):
                rows[cid].order = i
            ShowcaseCourse.objects.bulk_update(rows.values(), ["order"])
        return Response(
            ShowcaseCourseAdminSerializer(
                ShowcaseCourse.objects.all(), many=True, context={"request": request},
            ).data
        )


# ── Showcase categories (the Featured grid's filter tabs) ──────────

class ShowcaseCategoryAdminViewSet(viewsets.ModelViewSet):
    """Full CRUD for the homepage's filter tabs.

    No `RecordsRevisions`: revisions record what a person did to a piece of
    *content*, and this is taxonomy — the same reasoning that keeps migrations
    and seeds out of ContentRevision.
    """

    queryset = ShowcaseCategory.objects.all()
    serializer_class = ShowcaseCategoryAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = None

    def perform_destroy(self, instance):
        """Refuses while cards still carry the slug.

        `categories` is a JSON list of slugs with no FK, so deleting a row
        cannot cascade — the slug would simply be orphaned in every tagged
        card, and each of those cards would then fail clean() on its next save
        with "Unknown category". Hiding is almost always what was meant, so the
        message says so.
        """
        in_use = [
            cats for cats in ShowcaseCourse.objects.values_list("categories", flat=True)
            if isinstance(cats, list) and instance.slug in cats
        ]
        if in_use:
            n = len(in_use)
            raise DRFValidationError({
                "detail": f"{n} card{'' if n == 1 else 's'} still use this tab. "
                          f"Untag them first, or switch the tab off "
                          f"(is_active) to hide it without breaking them.",
                "card_count": n,
            })
        instance.delete()


# ── Editor-uploaded images (rich-text body content) ────────────────

class ContentImageAdminViewSet(viewsets.ModelViewSet):
    """Media library for rich-text editor images — full CRUD, distinct from
    the per-model `cover`/`image` fields elsewhere (a post body can embed
    many images). Upload a file, get back its URL + metadata; list/search
    to reuse an already-uploaded image instead of re-uploading."""
    queryset = ContentImage.objects.all().order_by("-created_at")
    serializer_class = ContentImageAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(alt_text__icontains=q) | Q(file__icontains=q)
            )
        return qs

    def perform_create(self, serializer):
        serializer.save(uploaded_by=self.request.user)


# ── Homepage content ──────────────────────────────────────────────

class HomeContentBlockAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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


class HomeListItemAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
    queryset = HomeListItem.objects.all()
    serializer_class = HomeListItemAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = AdminPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]  # `image` upload

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("section"):
            qs = qs.filter(section=p["section"])
        if p.get("variant"):
            qs = qs.filter(variant=p["variant"])
        return qs


class HomeFloaterAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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


class HomeSectionOrderAdminViewSet(
    mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    """List + per-row is_visible toggle, plus one atomic bulk `reorder`
    action. No create/destroy — the row set is fixed to HOMEPAGE_SECTIONS
    and seeded by migration; `section` itself is read-only on the
    serializer, so an update can only change `order`/`is_visible`."""

    queryset = HomeSectionOrder.objects.all()
    serializer_class = HomeSectionOrderAdminSerializer
    permission_classes = [IsContentEditor]
    pagination_class = None

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """POST {"sections": ["hero", "featured_courses", ...]} — the full
        list of section keys in the desired order. Must be exactly the
        existing set (no missing/extra/duplicate keys) so a stale admin tab
        can never silently drop a section from the homepage."""
        sections = request.data.get("sections")
        if not isinstance(sections, list) or not sections:
            return Response(
                {"detail": "sections must be a non-empty list of section keys."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        existing = set(HomeSectionOrder.objects.values_list("section", flat=True))
        given = set(sections)
        if len(sections) != len(given) or given != existing:
            return Response(
                {"detail": "sections must contain each existing section exactly once.",
                 "missing": sorted(existing - given), "unexpected": sorted(given - existing)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            rows = {r.section: r for r in HomeSectionOrder.objects.select_for_update()}
            for i, section in enumerate(sections):
                rows[section].order = i
            HomeSectionOrder.objects.bulk_update(rows.values(), ["order"])
        return Response(HomeSectionOrderAdminSerializer(
            HomeSectionOrder.objects.all(), many=True,
        ).data)


# ── Blog posts ────────────────────────────────────────────────────

class BlogPostAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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
        if p.get("locale"):
            qs = qs.filter(locale=p["locale"])
        if p.get("translation_group"):
            qs = qs.filter(translation_group=p["translation_group"])
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
        super().perform_create(serializer)          # saves + records
        instance = serializer.instance
        if not instance.author_id:
            instance.author = self.request.user
            instance.save(update_fields=["author"])
        _sync_tags(instance, tags)

    def _snapshot_revision(self, post, reason=""):
        # body_html_source is not a usable undo path — models.py's save()
        # reassigns it from the incoming payload on every write, before
        # sanitization, so it only ever holds a copy of the save currently
        # in flight, never the version being replaced.
        BlogRevision.objects.create(
            post=post,
            body_html=post.body_html,
            body_blocks=post.body_blocks,
            body_theme=post.body_theme,
            created_by=self.request.user if self.request.user.is_authenticated else None,
            reason=reason,
        )

    def perform_update(self, serializer):
        # Snapshot the PRE-update state before serializer.save() overwrites it.
        # Deliberately re-fetched from the DB rather than read off
        # serializer.instance: FullCleanMixin.validate() (admin_serializers.py)
        # setattr()s the incoming payload onto that exact instance object
        # during serializer.is_valid() — which DRF's UpdateModelMixin.update()
        # runs BEFORE perform_update() is ever called — so by this point
        # serializer.instance already holds the NEW values in memory, even
        # though nothing has been saved yet. A fresh query is the only copy
        # of the true pre-update row left.
        self._snapshot_revision(
            BlogPost.objects.get(pk=serializer.instance.pk),
            reason=self.request.data.get("revision_reason", ""),
        )
        tags = serializer.validated_data.pop("tags", None)
        super().perform_update(serializer)          # saves + records
        _sync_tags(serializer.instance, tags)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        post = self.get_object()
        before = snapshot_of(post)
        post.status = PublishStatus.PUBLISHED
        post.publish_at = timezone.now()
        post.save()
        self._record_status_action(post, before)
        return Response(self.get_serializer(post).data)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        post = self.get_object()
        before = snapshot_of(post)
        post.status = PublishStatus.DRAFT
        post.save()
        self._record_status_action(post, before)
        return Response(self.get_serializer(post).data)

    # `translation_group`/`locale` on the new row are assigned HERE,
    # server-side, from the source row already looked up via get_object() —
    # never trusted from the request body — so a client can't graft a new
    # post onto an arbitrary existing translation group by guessing a UUID.
    @action(detail=True, methods=["post"], url_path="duplicate-translation")
    def duplicate_translation(self, request, pk=None):
        source = self.get_object()
        locale = request.data.get("locale")
        if locale not in Locale.values:
            return Response({"detail": "Invalid locale."}, status=status.HTTP_400_BAD_REQUEST)
        if locale == source.locale:
            return Response(
                {"detail": "Pick a different locale than the source post."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if BlogPost.objects.filter(
            translation_group=source.translation_group, locale=locale
        ).exists():
            return Response(
                {"detail": f"A {locale} translation already exists for this post."},
                status=status.HTTP_409_CONFLICT,
            )
        # Slug copied verbatim (not blank) — same-slug-across-locale is the
        # convention, disambiguated by the public URL's /hi/ prefix, not by
        # the slug string. Title/excerpt/body are left as the English text
        # too, as a translation starting point rather than a blank editor —
        # a translator overwrites it, same spirit as the plain "Duplicate"
        # action in BlogPosts.jsx leaving content in place to edit from.
        new_post = BlogPost.objects.create(
            translation_group=source.translation_group,
            locale=locale,
            title=source.title,
            slug=source.slug,
            class_level=source.class_level,
            subject=source.subject,
            chapter_number=source.chapter_number,
            excerpt=source.excerpt,
            cover=source.cover,
            body_html=source.body_html,
            body_blocks=source.body_blocks,
            body_theme=source.body_theme,
            trusted_html=source.trusted_html,
            author=request.user,
            is_featured=False,
            seo_title=source.seo_title,
            seo_description=source.seo_description,
            status=PublishStatus.DRAFT,
        )
        new_post.tags.set(source.tags.all())
        return Response(self.get_serializer(new_post).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def revisions(self, request, pk=None):
        post = self.get_object()
        rows = post.revisions.select_related("created_by")[:100]
        return Response([
            {
                "id": r.id,
                "created_at": r.created_at,
                "created_by": getattr(r.created_by, "username", None),
                "reason": r.reason,
                "reading_minutes_estimate": max(1, round(
                    len(re.sub(r"<[^>]+>", " ", r.body_html).split()) / 200
                )),
            }
            for r in rows
        ])

    # Restoring writes a FRESH revision of the current (about-to-be-replaced)
    # state first — so undoing an undo is always possible — then PUTs the old
    # body_* fields back through the normal serializer path rather than
    # mutating the row directly, so it goes through the exact same
    # sanitize/validate/reading_minutes/cache-bump logic as any other save.
    @action(detail=True, methods=["post"], url_path="revisions/(?P<revision_id>[^/.]+)/restore")
    def restore_revision(self, request, pk=None, revision_id=None):
        post = self.get_object()
        try:
            rev = post.revisions.get(pk=revision_id)
        except BlogRevision.DoesNotExist:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)

        self._snapshot_revision(post, reason=f"Before restoring revision #{rev.id}")

        serializer = self.get_serializer(post, data={
            "body_html": rev.body_html,
            "body_blocks": rev.body_blocks,
            "body_theme": rev.body_theme,
        }, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self.get_serializer(post).data)

# ── Current affairs ───────────────────────────────────────────────

class CurrentAffairAdminViewSet(RecordsRevisions, viewsets.ModelViewSet):
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
        super().perform_create(serializer)          # saves + records
        _sync_tags(serializer.instance, tags)

    def perform_update(self, serializer):
        tags = serializer.validated_data.pop("tags", None)
        super().perform_update(serializer)          # saves + records
        _sync_tags(serializer.instance, tags)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        obj = self.get_object()
        before = snapshot_of(obj)
        obj.status = PublishStatus.PUBLISHED
        obj.publish_at = timezone.now()
        obj.save()
        self._record_status_action(obj, before)
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        obj = self.get_object()
        before = snapshot_of(obj)
        obj.status = PublishStatus.DRAFT
        obj.save()
        self._record_status_action(obj, before)
        return Response(self.get_serializer(obj).data)
