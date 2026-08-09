"""skills/admin_views.py — moderation surfaces for reviews and listings.

`ExpertReview.is_public` and `SkillListing.is_suspended` both existed on the
models with nothing in the admin dashboard able to flip them. Until now a
defamatory review could only be removed by its own author or from a shell.

    GET   /skill/admin/reviews/?flagged=1&expert=<id>   the queue
    PATCH /skill/admin/reviews/<id>/                    { is_public }
    GET   /skill/admin/listings/                        every listing
    POST  /skill/admin/listings/<id>/suspend/           { action, reason }
    GET   /skill/admin/moderation-flags/                the auto-raised queue
    POST  /skill/admin/moderation-flags/<id>/resolve/
"""
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from .listing_models import ListingModerationFlag, SkillListing
from .moderation import resolve_flag
from .review_models import ExpertReview
from .review_views import recalc_expert_rating, recalc_listing_rating


def _review_row(r):
    return {
        "id":          str(r.id),
        "rating":      r.rating,
        "body":        r.body,
        "is_public":   r.is_public,
        "is_edited":   r.is_edited,
        "created_at":  r.created_at,
        "expert_id":   str(r.expert_id),
        "expert_name": r.expert.display_name(),
        "reviewer":    (r.learner_profile.display_name
                        or r.learner_profile.full_name or "Student"),
        "listing_id":  str(r.session.listing_id) if (r.session_id and r.session.listing_id) else None,
    }


class AdminReviewListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = ExpertReview.objects.select_related(
            "expert", "learner_profile", "session"
        ).order_by("-created_at")
        if request.query_params.get("hidden") in ("1", "true"):
            qs = qs.filter(is_public=False)
        if expert_id := request.query_params.get("expert"):
            qs = qs.filter(expert_id=expert_id)
        if listing_id := request.query_params.get("listing"):
            qs = qs.filter(session__listing_id=listing_id)
        return Response([_review_row(r) for r in qs[:300]])


class AdminReviewDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, review_id):
        review = ExpertReview.objects.select_related(
            "expert", "learner_profile", "session"
        ).filter(id=review_id).first()
        if not review:
            raise NotFound("Review not found.")
        if "is_public" not in request.data:
            raise ValidationError("Send is_public.")
        review.is_public = bool(request.data["is_public"])
        review.save(update_fields=["is_public"])
        # Hiding a review must change the averages it fed, or a 1-star review
        # stays in the expert's rating after it has been taken down.
        recalc_expert_rating(review.expert)
        recalc_listing_rating(review.session.listing if review.session_id else None)
        return Response(_review_row(review))


def _listing_row(l):
    return {
        "id":             str(l.id),
        "title":          l.title,
        "expert_id":      str(l.expert_id),
        "expert_name":    l.expert.display_name(),
        "category_label": l.category.label if l.category_id else None,
        "price_rupees":   l.price_rupees,
        "rating":         l.rating,
        "sessions_count": l.sessions_count,
        "is_active":      l.is_active,
        "is_suspended":   l.is_suspended,
        "created_at":     l.created_at,
    }


class AdminListingListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = SkillListing.objects.select_related("expert", "category").order_by("-created_at")
        if request.query_params.get("suspended") in ("1", "true"):
            qs = qs.filter(is_suspended=True)
        if expert_id := request.query_params.get("expert"):
            qs = qs.filter(expert_id=expert_id)
        return Response([_listing_row(l) for l in qs[:300]])


class SuspendListingView(APIView):
    """POST { "action": "suspend" | "unsuspend", "reason": "…" }

    Suspension is an admin-only state: the teacher's own pause switch cannot
    lift it (see TeacherListingDetailView.patch).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, listing_id):
        listing = SkillListing.objects.select_related("expert", "category").filter(
            id=listing_id
        ).first()
        if not listing:
            raise NotFound("Skill not found.")
        action = (request.data.get("action") or "suspend").lower()
        if action == "suspend":
            listing.is_suspended = True
        elif action == "unsuspend":
            listing.is_suspended = False
        else:
            raise ValidationError("action must be 'suspend' or 'unsuspend'.")
        listing.save(update_fields=["is_suspended", "updated_at"])
        listing.expert.sync_primary_category()
        return Response(_listing_row(listing))


class AdminModerationFlagListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = ListingModerationFlag.objects.select_related("expert", "listing")
        if request.query_params.get("open", "1") in ("1", "true"):
            qs = qs.filter(is_open=True)
        return Response([
            {
                "id":            str(f.id),
                "reason":        f.reason,
                "detail":        f.detail,
                "expert_id":     str(f.expert_id),
                "expert_name":   f.expert.display_name(),
                "listing_id":    str(f.listing_id) if f.listing_id else None,
                "listing_title": f.listing.title if f.listing_id else None,
                "is_open":       f.is_open,
                "created_at":    f.created_at,
            }
            for f in qs[:300]
        ])


class AdminModerationFlagResolveView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, flag_id):
        flag = ListingModerationFlag.objects.filter(id=flag_id).first()
        if not flag:
            raise NotFound("Flag not found.")
        resolve_flag(flag)
        return Response({"id": str(flag.id), "is_open": flag.is_open})
