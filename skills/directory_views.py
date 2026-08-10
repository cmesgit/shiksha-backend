"""skills/directory_views.py — the public browse endpoint, filtered.

Replaces ExpertListView in skills/views.py. That view accepted cat, search,
offline, pincode, district and state, and returned the whole queryset
unpaginated. This version adds the filters the redesign exposes, fixes search,
prefetches listings for the multi-skill row, and paginates.

    GET /skill/teachers/?cat=&search=&mode=&district=&pincode=&state=
                        &price_max=&lang=&min_rating=&min_experience=
                        &has_video=&available_week=&sort=&page=

MIN_REVIEWS
    An expert with fewer than 5 public reviews has no trustworthy average.
    They are still LISTED, but they are held out of ?sort=rating rather than
    topping it on the strength of one 5-star review. The serializer sends
    reviews_count so the client can withhold the number too.
"""
from django.db.models import Count, F, Min, Prefetch, Q
from django.db.models.functions import Coalesce
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .listing_models import SkillListing
from .models import ExpertProfile
from .serializers import ExpertCardSerializer

MIN_REVIEWS = 5
PAGE_SIZE = 20


class DirectoryPagination(PageNumberPagination):
    page_size = PAGE_SIZE
    page_size_query_param = "page_size"
    max_page_size = 60


def _listing_prefetch():
    """Listings for the multi-skill row, with their review counts annotated.

    Annotating here rather than in SkillListingCardSerializer keeps the whole
    page at a fixed query count instead of one query per listing per expert.
    """
    qs = (
        SkillListing.objects
        .select_related("category")
        .prefetch_related("slot_keys")
        .annotate(public_reviews=Count(
            "sessions__review",
            filter=Q(sessions__review__is_public=True),
            distinct=True,
        ))
        .order_by("order", "-created_at")
    )
    return Prefetch("listings", queryset=qs)


class ExpertListView(APIView):
    """Public. No auth — browsing must work for a logged-out visitor."""
    permission_classes = [AllowAny]

    def get(self, request):
        p = request.query_params
        qs = (
            ExpertProfile.objects
            .filter(is_listed=True, teacher_profile__isnull=False)
            .select_related("category", "teacher_profile__user")
            .prefetch_related("categories", _listing_prefetch())
            .annotate(
                public_reviews=Count(
                    "reviews", filter=Q(reviews__is_public=True), distinct=True
                ),
                from_price=Min(
                    "listings__price_paise",
                    filter=Q(listings__is_active=True, listings__is_suspended=False),
                ),
            )
            .annotate(effective_price=Coalesce("from_price", "hourly_rate"))
        )

        # ── category ──────────────────────────────────────────────────────
        cat = p.get("cat") or p.get("category")
        if cat and cat != "all":
            qs = qs.filter(Q(category__slug=cat) | Q(categories__slug=cat)).distinct()

        # ── search ────────────────────────────────────────────────────────
        # BUG FIX: the previous implementation matched ONLY headline, while the
        # placeholder promises "guitar", "welding", "spoken English" — i.e.
        # skill tags and subject. Search every field the copy implies.
        q = (p.get("search") or "").strip()
        if q:
            qs = qs.filter(
                Q(headline__icontains=q)
                | Q(bio__icontains=q)
                | Q(subject_description__icontains=q)
                | Q(skill_tags__icontains=q)
                | Q(category__label__icontains=q)
                | Q(categories__label__icontains=q)
                # Only LIVE listings can match. A paused skill surfacing here
                # sends a learner to a teacher for something they can't book.
                | Q(listings__is_active=True, listings__title__icontains=q)
                | Q(listings__is_active=True, listings__skill_tags__icontains=q)
                | Q(teacher_profile__user__first_name__icontains=q)
                | Q(teacher_profile__user__last_name__icontains=q)
            ).distinct()

        # ── class mode / location ─────────────────────────────────────────
        mode = p.get("mode")
        if mode == ExpertProfile.MODE_ONLINE:
            qs = qs.filter(class_mode=ExpertProfile.MODE_ONLINE)
        elif mode in (ExpertProfile.MODE_HOME, ExpertProfile.MODE_TRAVEL):
            qs = qs.filter(class_mode=mode)
        elif (p.get("offline") or "").lower() in ("1", "true", "yes"):
            qs = qs.filter(
                class_mode__in=[ExpertProfile.MODE_HOME, ExpertProfile.MODE_TRAVEL]
            )

        # Online teachers are location-independent — excluding them from a
        # district filter is the single most common way a directory returns
        # "no results" for a learner who had options all along.
        online = Q(class_mode=ExpertProfile.MODE_ONLINE)
        if (d := (p.get("district") or "").strip()) and d != "all":
            qs = qs.filter(Q(district__iexact=d) | online)
        if pin := (p.get("pincode") or "").strip():
            qs = qs.filter(Q(pincode=pin) | online)
        if (st := (p.get("state") or "").strip()) and st != "all":
            qs = qs.filter(Q(state__iexact=st) | online)

        # ── price · language · rating · experience ────────────────────────
        if price_max := p.get("price_max"):
            try:
                paise = int(price_max) * 100
            except (TypeError, ValueError):
                paise = None
            if paise is not None:
                qs = qs.filter(Q(hourly_rate__lte=paise) | Q(from_price__lte=paise))
        if lang := (p.get("lang") or "").strip():
            qs = qs.filter(languages__icontains=lang)
        if min_rating := p.get("min_rating"):
            try:
                qs = qs.filter(
                    rating__gte=float(min_rating), public_reviews__gte=MIN_REVIEWS
                )
            except (TypeError, ValueError):
                pass
        if min_exp := p.get("min_experience"):
            try:
                qs = qs.filter(experience_years__gte=int(min_exp))
            except (TypeError, ValueError):
                pass

        # ── intro video · availability ────────────────────────────────────
        if (p.get("has_video") or "").lower() in ("1", "true", "yes"):
            qs = qs.filter(
                Q(intro_video_status=4) | Q(listings__intro_video_status=4)
            ).distinct()

        sort = p.get("sort", "recommended")
        rows = qs.order_by(*_ORDER.get(sort, _ORDER["recommended"]))

        # ── the two Python-side passes ────────────────────────────────────
        # `available_week` reads a JSONField grid, and "recommended" depends on
        # is_advertised() (billing mode + subscription), so neither can be
        # expressed in SQL. Both materialise the queryset. Fine at ~128 experts
        # and NOT at 10x that — see the note on _open_slots below.
        if (p.get("available_week") or "").lower() in ("1", "true", "yes"):
            rows = [e for e in rows if _open_slots(e) > 0]
        if sort == "recommended":
            rows = list(rows)
            rows = ([e for e in rows if e.is_advertised()]
                    + [e for e in rows if not e.is_advertised()])

        page = DirectoryPagination()
        chunk = page.paginate_queryset(rows, request, view=self)
        data = ExpertCardSerializer(chunk, many=True, context={"request": request}).data
        return page.get_paginated_response(data)


# Advertised/featured first (the CMS pays for that), then trustworthy ratings,
# then volume. F(...).desc(nulls_last=True) keeps unrated experts from sorting
# above rated ones on a NULL.
_ORDER = {
    "recommended": ("-is_featured", "-reach_count",
                    F("rating").desc(nulls_last=True), "-sessions_count"),
    "rating":      ("-is_featured", F("rating").desc(nulls_last=True), "-public_reviews"),
    "price_asc":   ("effective_price",),
    "price_desc":  ("-effective_price",),
    "sessions":    ("-sessions_count",),
    "experience":  (F("experience_years").desc(nulls_last=True),),
    "newest":      ("-created_at",),
}


def _open_slots(expert) -> int:
    """Open minus booked in ExpertProfile.availability_slots.

    NOTE: availability lives in a JSONField, so ?available_week=1 cannot be
    expressed in SQL and forces the queryset into Python. That is acceptable
    at ~128 experts and NOT at 10x that. Before the directory grows, move the
    grid into a real AvailabilitySlot table (expert, weekday, hour, is_booked)
    and this becomes an ordinary .filter().
    """
    grid = expert.availability_slots or {}
    return len(set(grid.get("open", [])) - set(grid.get("booked", [])))


class DirectoryStatsView(APIView):
    """GET /skill/directory-stats/ — the hero "at a glance" panel.

    Four numbers, cached for an hour. Without this the panel ships hardcoded
    constants that quietly become lies the first time an expert signs up.
    """
    permission_classes = [AllowAny]
    CACHE_KEY = "skill:directory-stats:v1"
    CACHE_SECONDS = 3600

    def get(self, request):
        from django.core.cache import cache

        cached = cache.get(self.CACHE_KEY)
        if cached:
            return Response(cached)

        listed = ExpertProfile.objects.filter(is_listed=True)
        prices = sorted(
            r // 100 for r in listed.values_list("hourly_rate", flat=True) if r
        )

        def pct(fraction):
            if not prices:
                return None
            return prices[min(len(prices) - 1, int(len(prices) * fraction))]

        from .models import SkillCategory
        payload = {
            "experts": listed.count(),
            "categories": SkillCategory.objects.filter(is_active=True).count(),
            "offline": listed.exclude(class_mode=ExpertProfile.MODE_ONLINE).count(),
            "price_p25": pct(0.25),
            "price_p75": pct(0.75),
        }
        cache.set(self.CACHE_KEY, payload, self.CACHE_SECONDS)
        return Response(payload)
