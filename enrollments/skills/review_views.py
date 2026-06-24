"""skills/review_views.py"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework import status

from accounts.auth_flow import get_active_profile
from .models import SkillSession, ExpertProfile
from .review_models import ExpertReview


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

        # Update cached expert rating.
        ep = sess.expert
        all_ratings = list(
            ExpertReview.objects.filter(expert=ep, is_public=True).values_list("rating", flat=True)
        )
        if all_ratings:
            ep.rating = round(sum(all_ratings) / len(all_ratings), 2)
            ep.save(update_fields=["rating"])

        return Response(
            {"id": str(review.id), "rating": review.rating, "ok": True},
            status=status.HTTP_201_CREATED,
        )


class ExpertReviewListView(APIView):
    """GET /skill/teachers/<expert_id>/reviews/  — public list of reviews."""
    permission_classes = [AllowAny]

    def get(self, request, expert_id):
        expert = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not expert:
            raise NotFound("Expert not found.")
        reviews = ExpertReview.objects.filter(
            expert=expert, is_public=True
        ).select_related("learner_profile").order_by("-created_at")

        data = [
            {
                "id":         str(r.id),
                "rating":     r.rating,
                "body":       r.body,
                "created_at": r.created_at,
                "reviewer":   r.learner_profile.display_name or r.learner_profile.full_name or "Student",
            }
            for r in reviews
        ]
        return Response({"count": len(data), "reviews": data})


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
