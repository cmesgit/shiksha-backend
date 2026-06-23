"""skills/review_models.py — post-session learner reviews."""
import uuid
from django.db import models


class ExpertReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session = models.OneToOneField(
        "skills.SkillSession", on_delete=models.CASCADE, related_name="review"
    )
    expert = models.ForeignKey(
        "skills.ExpertProfile", on_delete=models.CASCADE, related_name="reviews"
    )
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile", on_delete=models.CASCADE, related_name="expert_reviews"
    )
    rating = models.PositiveSmallIntegerField()          # 1–5
    body   = models.TextField(blank=True)
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["expert", "is_public"])]

    def __str__(self):
        return f"Review {self.rating}★ → {self.expert}"
