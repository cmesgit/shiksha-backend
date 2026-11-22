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
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

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


def _public_teacher_name(user):
    """A name safe to show to an anonymous visitor, or None.

    Mirrors courses/admin_academy_views.py's `_teacher_name`, which is
    admin-only and falls back to `user.email` as a last resort — that
    fallback is exactly the PII leak a public feed card must not repeat, so
    it is dropped here rather than copied. A caller gets None (not an email)
    when no presentable name exists, and skips the card.
    """
    lp = getattr(user, "default_learner_profile", None)
    lp = lp() if callable(lp) else None
    if lp and getattr(lp, "full_name", ""):
        return lp.full_name
    full = (user.get_full_name() or "").strip()
    return full or user.username or None


class HomeHeroHighlightsView(APIView):
    """GET /api/content/home/hero-highlights/ — the hero's stat counters and
    "just published" feed (homepage hero v3, floating-stack/orbit design).

    Cached like DirectoryStatsView (skills/directory_views.py): a flat TTL,
    no version-bump invalidation — none of the five source models here
    (User/TeacherProfile, Course, StudyMaterial, Quiz, LiveSession) are wired
    into content/cache.py's bump list, so a signal-based scheme would need
    new signals on four other apps for no real gain over a short TTL on an
    endpoint nobody needs to the second.
    """

    permission_classes = [AllowAny]
    CACHE_KEY = "content:home-hero-highlights:v1"
    CACHE_SECONDS = 1800  # 30 min
    NEW_WINDOW_DAYS = 30
    FEED_LIMIT_PER_TYPE = 4
    # Trims the merged, time-sorted feed to a sensible marquee length. Not a
    # correctness cap — every type already queried at most FEED_LIMIT_PER_TYPE
    # rows, so this can only drop the globally-oldest of those, never hide an
    # entire type.
    FEED_LIMIT_TOTAL = 16

    def get(self, request):
        cached = cache.get(self.CACHE_KEY)
        if cached is not None:
            return Response(cached)
        payload = {"stats": self._stats(), "feed": self._feed()}
        cache.set(self.CACHE_KEY, payload, self.CACHE_SECONDS)
        return Response(payload)

    def _stats(self):
        from accounts.models import TeacherProfile
        from courses.models import Course
        from courses.views import PUBLIC_COURSE_STATUSES
        from django.contrib.auth import get_user_model
        from quizzes.models import Quiz
        from quizzes.visibility import visible_quiz_q

        User = get_user_model()
        since = timezone.now() - timezone.timedelta(days=self.NEW_WINDOW_DAYS)

        # Approved academy teachers who currently hold at least one active
        # staffed subject. ONE filter() call across the multi-valued
        # relations (teaching_assignments, user_roles), then .distinct() —
        # chaining separate .filter() calls on a multi-valued relation
        # produces independent joins and inflates the count. See
        # courses/admin_academy_views.py's own documented version of this
        # trap.
        teachers_qs = User.objects.filter(
            teacher_profile__academy_status=TeacherProfile.TRACK_APPROVED,
            user_roles__role__name="TEACHER",
            user_roles__is_active=True,
            teaching_assignments__is_active=True,
        ).distinct()
        # "New" is an approximation: TeacherProfile has no approved_at field,
        # only created_at (profile creation, not approval). Fine for a
        # decorative hero stat; would not be fine for anything audit-shaped.
        teachers_new = teachers_qs.filter(
            teacher_profile__created_at__gte=since,
        ).count()

        courses_qs = Course.objects.filter(status__in=PUBLIC_COURSE_STATUSES)
        courses_new = courses_qs.filter(created_at__gte=since).count()

        # Exists()-based, not a join — see quizzes/visibility.py's module
        # docstring on why this must never be "simplified" into one.
        # batch_id=None degrades to course-wide-only, which is the correct,
        # fail-closed choice for an anonymous/public count: a batch-scoped
        # quiz is enrolment-gated content, not something a visitor can see.
        quizzes_qs = Quiz.objects.filter(
            visible_quiz_q(None),
            subject__course__status__in=PUBLIC_COURSE_STATUSES,
        )
        quizzes_new = quizzes_qs.filter(created_at__gte=since).count()

        return {
            "teachers": {"label": "Teachers", "total": teachers_qs.count(), "new_count": teachers_new},
            "courses": {"label": "Courses", "total": courses_qs.count(), "new_count": courses_new},
            "quizzes": {"label": "Quizzes", "total": quizzes_qs.count(), "new_count": quizzes_new},
        }

    def _feed(self):
        from accounts.models import TeacherProfile
        from courses.models import Course
        from courses.views import PUBLIC_COURSE_STATUSES
        from django.contrib.auth import get_user_model
        from livestream.models import LiveSession
        from materials.models import StudyMaterial
        from quizzes.models import Quiz
        from quizzes.visibility import visible_quiz_q

        User = get_user_model()
        limit = self.FEED_LIMIT_PER_TYPE
        items = []

        teachers = (
            User.objects.filter(
                teacher_profile__academy_status=TeacherProfile.TRACK_APPROVED,
                user_roles__role__name="TEACHER",
                user_roles__is_active=True,
                teaching_assignments__is_active=True,
            )
            .distinct()
            .select_related("teacher_profile")
            .order_by("-teacher_profile__created_at")[:limit]
        )
        for user in teachers:
            name = _public_teacher_name(user)
            if not name:
                continue
            profile = user.teacher_profile
            subject = profile.get_subject_display() if profile.subject else ""
            years = profile.teaching_experience_years
            if subject and years:
                subtitle = f"{subject} · {years} years teaching"
            else:
                subtitle = subject or "New mentor on ShikshaCom"
            items.append({
                "type": "new_teacher",
                "title": f"{name} joined",
                "subtitle": subtitle,
                "timestamp": profile.created_at,
            })

        courses = (
            Course.objects.filter(status__in=PUBLIC_COURSE_STATUSES)
            .select_related("board")
            .order_by("-created_at")[:limit]
        )
        for course in courses:
            bits = []
            if course.board_id:
                bits.append(course.board.name)
            if course.class_level:
                bits.append(f"Class {course.class_level}")
            items.append({
                "type": "new_course",
                "title": course.title,
                "subtitle": " · ".join(bits) or "Now enrolling",
                "timestamp": course.created_at,
            })

        materials = (
            StudyMaterial.objects.filter(subject__course__status__in=PUBLIC_COURSE_STATUSES)
            .select_related("subject", "chapter")
            .order_by("-created_at")[:limit]
        )
        for material in materials:
            bits = [material.subject.name]
            if material.chapter_id:
                bits.append(material.chapter.title)
            items.append({
                "type": "new_material",
                "title": material.title,
                "subtitle": " · ".join(bits),
                "timestamp": material.created_at,
            })

        quizzes = (
            Quiz.objects.filter(
                visible_quiz_q(None),
                subject__course__status__in=PUBLIC_COURSE_STATUSES,
            )
            .select_related("subject")
            .order_by("-created_at")[:limit]
        )
        for quiz in quizzes:
            bits = [quiz.subject.name]
            if quiz.time_limit_minutes:
                bits.append(f"{quiz.time_limit_minutes} min")
            items.append({
                "type": "new_quiz",
                "title": quiz.title,
                "subtitle": " · ".join(bits),
                "timestamp": quiz.created_at,
            })

        now = timezone.now()
        sessions = (
            LiveSession.objects.filter(
                course__status__in=PUBLIC_COURSE_STATUSES,
                status__in=[
                    LiveSession.STATUS_SCHEDULED, LiveSession.STATUS_WAITING,
                    LiveSession.STATUS_LIVE, LiveSession.STATUS_PAUSED,
                    LiveSession.STATUS_RECONNECTING,
                ],
                end_time__gte=now,
            )
            .select_related("subject")
            .order_by("start_time")[:limit]
        )
        for session in sessions:
            live_now = session.computed_status() == LiveSession.STATUS_LIVE
            items.append({
                "type": "live_session",
                "title": session.title,
                "subtitle": f"{session.subject.name} · {'Live now' if live_now else 'Starting soon'}",
                "timestamp": session.start_time,
            })

        items.sort(key=lambda item: item["timestamp"], reverse=True)
        trimmed = items[: self.FEED_LIMIT_TOTAL]
        for item in trimmed:
            item["timestamp"] = item["timestamp"].isoformat()
        return trimmed
