"""skills/review_views.py"""
from django.db.models import Avg, Count
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework import status

from accounts.auth_flow import get_active_profile
from .models import SkillSession, ExpertProfile
from .review_models import ExpertReview


# ── Cached-rating recomputation ──────────────────────────────────────────
# Both caches are recomputed from scratch on every review write. The expert's
# headline average is every public review across ALL their listings, so it
# stays a genuine weighted average rather than the mean of per-skill means.

def recalc_expert_rating(expert):
    agg = ExpertReview.objects.filter(
        expert=expert, is_public=True
    ).aggregate(avg=Avg("rating"))
    expert.rating = round(agg["avg"], 2) if agg["avg"] is not None else None
    expert.save(update_fields=["rating"])


def recalc_listing_rating(listing):
    """Per-skill average + completed-session count. No-op without a listing —
    sessions booked before multi-skill existed may still have listing=None."""
    if listing is None:
        return
    agg = ExpertReview.objects.filter(
        session__listing=listing, is_public=True
    ).aggregate(avg=Avg("rating"))
    listing.rating = round(agg["avg"], 2) if agg["avg"] is not None else None
    listing.sessions_count = listing.sessions.filter(
        status=SkillSession.STATUS_COMPLETED
    ).count()
    listing.save(update_fields=["rating", "sessions_count"])


def serialize_public_review(r):
    """What the public review list sends.

    The old payload dropped created_at, is_edited and the session — so a review
    read as current forever, an edited review was presented as the original,
    and nothing said the reviewer had actually taken the class.
    """
    return {
        "id":         str(r.id),
        "rating":     r.rating,
        "body":       r.body,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "is_edited":  r.is_edited,
        # SkillSession has no `topic` column; the booking note is what the
        # learner wrote when they requested the session, which is the closest
        # honest answer to "what was this about".
        "topic":      (r.session.note or "").strip()[:60] if r.session_id else "",
        "listing":    str(r.session.listing_id) if (r.session_id and r.session.listing_id) else None,
        "reviewer":   (r.learner_profile.display_name
                       or r.learner_profile.full_name or "Student").split(" ")[0],
    }


class SubmitReviewView(APIView):
    """
    POST /skill/sessions/<session_id>/review/
    { rating (1-5), body (optional) }
    Only the learner who had the session can review; only once per session;
    only after the session is completed.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        sess = SkillSession.objects.filter(
            id=session_id, learner_profile=learner
        ).select_related("expert").first()
        if not sess:
            raise NotFound("Session not found.")
        if sess.status != SkillSession.STATUS_COMPLETED:
            raise ValidationError("You can only review a completed session.")
        if ExpertReview.objects.filter(session=sess).exists():
            raise ValidationError("You already reviewed this session.")

        rating = request.data.get("rating")
        try:
            rating = int(rating)
            if not 1 <= rating <= 5:
                raise ValueError
        except (TypeError, ValueError):
            raise ValidationError({"rating": "Rating must be 1–5."})

        body = (request.data.get("body") or "").strip()

        review = ExpertReview.objects.create(
            session=sess,
            expert=sess.expert,
            learner_profile=learner,
            rating=rating,
            body=body,
        )

        recalc_expert_rating(sess.expert)
        recalc_listing_rating(sess.listing)

        return Response(
            {"id": str(review.id), "rating": review.rating, "ok": True},
            status=status.HTTP_201_CREATED,
        )


class ExpertReviewListView(APIView):
    """GET /skill/teachers/<expert_id>/reviews/[?listing=<uuid>]

    Public. Returns the reviews plus the star distribution the breakdown panel
    renders, and `average` — withheld (null) under MIN_REVIEWS, because one
    5-star review is not a 5.0 rating.
    """
    permission_classes = [AllowAny]
    MIN_REVIEWS = 5

    def get(self, request, expert_id):
        expert = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not expert:
            raise NotFound("Expert not found.")

        qs = ExpertReview.objects.filter(expert=expert, is_public=True)
        listing_id = request.query_params.get("listing")
        if listing_id:
            qs = qs.filter(session__listing_id=listing_id)
        reviews = qs.select_related("learner_profile", "session").order_by("-created_at")

        data = [serialize_public_review(r) for r in reviews]
        buckets = dict(
            qs.values_list("rating").annotate(n=Count("id")).values_list("rating", "n")
        )
        distribution = {str(star): buckets.get(star, 0) for star in (5, 4, 3, 2, 1)}
        agg = qs.aggregate(avg=Avg("rating"))["avg"]

        return Response({
            "count": len(data),
            "reviews": data,
            "distribution": distribution,
            "average": round(agg, 2) if (agg is not None and len(data) >= self.MIN_REVIEWS) else None,
            "min_reviews": self.MIN_REVIEWS,
        })


class MyReviewableSessionsView(APIView):
    """
    GET /skill/my-reviewable-sessions/
    Returns the learner's completed sessions that haven't been reviewed yet.
    The student dashboard shows a "Give a review" card for each.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        reviewed_ids = set(
            ExpertReview.objects.filter(learner_profile=learner).values_list("session_id", flat=True)
        )
        sessions = SkillSession.objects.filter(
            learner_profile=learner,
            status=SkillSession.STATUS_COMPLETED,
        ).select_related("expert").exclude(id__in=reviewed_ids)

        return Response([
            {
                "session_id":   str(s.id),
                "expert_id":    str(s.expert.id),
                "expert_name":  s.expert.display_name(),
                "completed_at": s.updated_at,
            }
            for s in sessions
        ])


class StudentMyReviewsView(APIView):
    """
    GET /skill/my-reviews/
    The reviews THIS learner has written, for the "My Reviews" nav page on the
    Learner Skill Dev dashboard. Each row is editable via MyReviewUpdateView.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        reviews = (
            ExpertReview.objects
            .filter(learner_profile=learner)
            .select_related("expert", "session")
            .order_by("-created_at")
        )

        data = [
            {
                "id":          str(r.id),
                "rating":      r.rating,
                "body":        r.body,
                "is_edited":   r.is_edited,
                "created_at":  r.created_at,
                "expert_id":   str(r.expert_id),
                "expert_name": r.expert.display_name(),
                "topic":       (r.session.note[:60] if (r.session_id and r.session.note) else "1-on-1 session"),
            }
            for r in reviews
        ]
        return Response({"count": len(data), "reviews": data})


class MyReviewUpdateView(APIView):
    """
    PATCH /skill/my-reviews/<review_id>/   { rating (1-5), body (optional) }
    Lets a learner edit a review they previously wrote. Recomputes the expert's
    cached average rating afterwards.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, review_id):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        try:
            review = ExpertReview.objects.select_related("expert").get(
                id=review_id, learner_profile=learner
            )
        except (ExpertReview.DoesNotExist, ValueError):
            raise NotFound("Review not found.")

        changed = []
        if "rating" in request.data:
            try:
                rating = int(request.data.get("rating"))
                if not 1 <= rating <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValidationError({"rating": "Rating must be 1–5."})
            review.rating = rating
            changed.append("rating")

        if "body" in request.data:
            review.body = (request.data.get("body") or "").strip()
            changed.append("body")

        if changed:
            review.is_edited = True
            review.save(update_fields=changed + ["is_edited"])

        recalc_expert_rating(review.expert)
        recalc_listing_rating(review.session.listing if review.session_id else None)

        return Response({
            "id":        str(review.id),
            "rating":    review.rating,
            "body":      review.body,
            "is_edited": review.is_edited,
            "ok":        True,
        })

    def delete(self, request, review_id):
        """DELETE /skill/my-reviews/<review_id>/ — design's Reviews screen
        Delete flow (README.md §8), permanent, then the expert's cached
        average rating is recomputed over the remaining reviews."""
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")
        try:
            review = ExpertReview.objects.select_related("expert").get(
                id=review_id, learner_profile=learner
            )
        except (ExpertReview.DoesNotExist, ValueError):
            raise NotFound("Review not found.")

        ep = review.expert
        # Read the listing BEFORE deleting — afterwards the session FK is gone.
        listing = review.session.listing if review.session_id else None
        review.delete()

        recalc_expert_rating(ep)
        recalc_listing_rating(listing)

        return Response(status=204)
