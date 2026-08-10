"""skills/listing_models.py — one bookable offering by an expert.

WHY THIS EXISTS
    ExpertProfile.categories is already M2M ("An expert can teach MORE THAN
    ONE subject"), but everything that SELLS a skill is single-valued on the
    profile: one headline, one hourly_rate, one bio, one subject_description,
    one intro video, one mastery_target. So a teacher could be TAGGED with
    three subjects while advertising exactly one offering.

    SkillListing makes an offering a first-class row. ExpertProfile keeps the
    person (photo, languages, location, education, badges, verification);
    SkillListing carries what varies per subject.

MIGRATION
    Data migration 0027_seed_skill_listings creates exactly one SkillListing
    per existing ExpertProfile from its current headline / hourly_rate /
    subject_description / intro video / mastery_target, and 0028 backfills
    every existing SkillSession onto it. The legacy fields stay on
    ExpertProfile, mirrored from the primary listing, so a mid-deploy app
    build never reads a null.
"""
import uuid

from django.db import models


class SkillListing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    expert = models.ForeignKey(
        "skills.ExpertProfile", on_delete=models.CASCADE, related_name="listings"
    )
    category = models.ForeignKey(
        "skills.SkillCategory", on_delete=models.PROTECT, related_name="listings"
    )

    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    skill_tags = models.JSONField(default=list, blank=True)   # ["Guitar", "Music theory"]

    # Paise, consistent with ExpertProfile.hourly_rate and the courses app.
    price_paise = models.PositiveIntegerField(default=0)

    cover = models.ImageField(upload_to="skills/listings/", null=True, blank=True)

    # Per-listing intro clip. Statuses reuse ExpertProfile's choices verbatim
    # so the existing Bunny status-polling code works unchanged.
    intro_video_bunny_id = models.CharField(max_length=255, blank=True)
    intro_video_status = models.IntegerField(null=True, blank=True)
    intro_video_thumbnail_url = models.URLField(blank=True)

    mastery_target = models.PositiveSmallIntegerField(default=3)

    # is_active    = the teacher paused it (reversible by the teacher)
    # is_suspended = an admin took it down (only an admin can lift it)
    is_active = models.BooleanField(default=True)
    is_suspended = models.BooleanField(default=False)

    # Cached, recomputed by review_views on every create/edit/delete — the same
    # pattern ExpertProfile.rating already uses.
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    sessions_count = models.PositiveIntegerField(default=0)

    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "-created_at"]
        indexes = [
            models.Index(fields=["expert", "is_active"]),
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.expert_id}"

    @property
    def price_rupees(self):
        return self.price_paise // 100

    @property
    def is_bookable(self):
        """Live to learners. Suspension beats the teacher's own toggle."""
        return self.is_active and not self.is_suspended and self.expert.is_listed

    def intro_video_embed_url(self):
        """Bunny iframe URL, or None. Mirrors ExpertProfile.intro_video_embed_url."""
        if not (self.intro_video_bunny_id and self.intro_video_status == 4):
            return None
        from django.conf import settings
        return f"{settings.BUNNY_EMBED}/{settings.BUNNY_LIBRARY_ID}/{self.intro_video_bunny_id}"


# ── NOTE: availability deliberately stays on ExpertProfile ────────────────
# Per-listing slot grids can double-book the same human. Slots remain
# ExpertProfile.availability_slots; a listing declares which slot keys it may
# be booked into via SkillListingSlot below. Booking still validates against
# the ONE expert grid, so two skills can never claim the same hour.
class SkillListingSlot(models.Model):
    listing = models.ForeignKey(
        SkillListing, on_delete=models.CASCADE, related_name="slot_keys"
    )
    slot_key = models.CharField(max_length=8)   # "<dayIndex>-<slotIndex>", e.g. "3-1"

    class Meta:
        unique_together = [("listing", "slot_key")]

    def __str__(self):
        return f"{self.listing_id} @ {self.slot_key}"


class ListingModerationFlag(models.Model):
    """Raised automatically (e.g. a burst of new listings) or by a report.

    `is_public` on ExpertReview and `is_suspended` on SkillListing both existed
    with no way for an admin to see WHY something should be looked at. This is
    that queue — see admin_views.AdminModerationQueueView.
    """
    REASON_LISTING_BURST = "listing_burst"
    REASON_REPORTED = "reported"
    REASON_CHOICES = [
        (REASON_LISTING_BURST, "More than three listings published in seven days"),
        (REASON_REPORTED, "Reported by a user"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expert = models.ForeignKey(
        "skills.ExpertProfile", on_delete=models.CASCADE, related_name="moderation_flags"
    )
    listing = models.ForeignKey(
        SkillListing, on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderation_flags",
    )
    reason = models.CharField(max_length=40, choices=REASON_CHOICES)
    detail = models.CharField(max_length=255, blank=True)
    is_open = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_open", "-created_at"])]

    def __str__(self):
        return f"{self.reason} → {self.expert_id}"
