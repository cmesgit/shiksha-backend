"""skills/listing_serializers.py — SkillListing read + write shapes."""
from rest_framework import serializers

from .listing_models import SkillListing
from .review_models import ExpertReview


class SkillListingCardSerializer(serializers.ModelSerializer):
    """Nested inside ExpertCardSerializer — what the directory row renders."""
    category_slug  = serializers.CharField(source="category.slug", read_only=True)
    category_label = serializers.CharField(source="category.label", read_only=True)
    price_rupees   = serializers.IntegerField(read_only=True)
    reviews_count  = serializers.SerializerMethodField()
    open_slots     = serializers.SerializerMethodField()
    intro_video_embed_url = serializers.SerializerMethodField()

    class Meta:
        model = SkillListing
        fields = [
            "id", "title", "description", "skill_tags",
            "category_slug", "category_label",
            "price_rupees", "rating", "reviews_count", "sessions_count",
            "mastery_target", "intro_video_status", "intro_video_thumbnail_url",
            "intro_video_embed_url",
            "is_active", "is_suspended", "open_slots", "order",
        ]

    def get_reviews_count(self, obj):
        # Annotated by the directory view so 20 rows cost one query, not 20.
        n = getattr(obj, "public_reviews", None)
        if n is not None:
            return n
        return ExpertReview.objects.filter(
            session__listing=obj, is_public=True
        ).count()

    def get_open_slots(self, obj):
        """Slot keys this listing may use that are still open on the expert."""
        grid = obj.expert.availability_slots or {}
        open_keys = set(grid.get("open", [])) - set(grid.get("booked", []))
        declared = {s.slot_key for s in obj.slot_keys.all()}
        return len(open_keys & declared) if declared else len(open_keys)

    def get_intro_video_embed_url(self, obj):
        return obj.intro_video_embed_url()


class SkillListingWriteSerializer(serializers.ModelSerializer):
    """Teacher-facing create/update. price_rupees in, price_paise stored."""
    price_rupees   = serializers.IntegerField(min_value=0, max_value=100000)
    category_label = serializers.CharField(source="category.label", read_only=True)
    category_slug  = serializers.CharField(source="category.slug", read_only=True)
    reviews_count  = serializers.SerializerMethodField()
    open_slots     = serializers.SerializerMethodField()

    class Meta:
        model = SkillListing
        fields = [
            "id", "title", "category", "category_label", "category_slug",
            "description", "skill_tags",
            "price_rupees", "mastery_target", "is_active",
            "intro_video_status", "intro_video_thumbnail_url",
            "rating", "reviews_count", "sessions_count", "open_slots",
            "is_suspended", "order",
        ]
        read_only_fields = ["id", "rating", "sessions_count", "is_suspended",
                            "intro_video_status", "intro_video_thumbnail_url",
                            "order"]

    def get_reviews_count(self, obj):
        return ExpertReview.objects.filter(
            session__listing=obj, is_public=True
        ).count()

    def get_open_slots(self, obj):
        grid = obj.expert.availability_slots or {}
        open_keys = set(grid.get("open", [])) - set(grid.get("booked", []))
        declared = {s.slot_key for s in obj.slot_keys.all()}
        return len(open_keys & declared) if declared else len(open_keys)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # price_rupees is a write field on the model's price_paise; echo the
        # stored value back rather than whatever was posted.
        data["price_rupees"] = instance.price_rupees
        return data

    def validate_title(self, v):
        v = (v or "").strip()
        if len(v) < 4:
            raise serializers.ValidationError("Give the skill a title students will recognise.")
        return v

    def validate_mastery_target(self, v):
        if not 1 <= int(v) <= 12:
            raise serializers.ValidationError("Pick between 1 and 12 sessions to mastery.")
        return v

    def validate_skill_tags(self, v):
        tags = [t.strip() for t in (v or []) if t and t.strip()]
        if len(tags) > 8:
            raise serializers.ValidationError("Eight tags is plenty — pick the ones students search for.")
        return tags

    def create(self, validated):
        validated["price_paise"] = validated.pop("price_rupees") * 100
        listing = super().create(validated)
        listing.expert.categories.add(listing.category)   # keep the M2M in step
        listing.expert.sync_primary_category()
        return listing

    def update(self, instance, validated):
        if "price_rupees" in validated:
            validated["price_paise"] = validated.pop("price_rupees") * 100
        listing = super().update(instance, validated)
        listing.expert.categories.add(listing.category)
        listing.expert.sync_primary_category()
        return listing
