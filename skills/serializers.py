"""
PLACEMENT: backend/backend/skills/serializers.py
ACTION:    Replace the entire file.

Adds to ExpertCardSerializer (public directory card):
  • location block (city/district/state/pincode) + class_mode/class_location
    so learners can find someone who teaches offline near them,
  • languages + subject_description,
  • advertising signals: advertised (bool), featured (bool), reach.
The expert's own UPI (pay_to) is deliberately NOT in the public card — it is
only surfaced to a learner after they book (booking response + session detail).

Unchanged: ExpertCardSerializer still exposes `teacher_profile_id`, which the
chat system's StartDirectView needs to open a 1-on-1 thread.
"""
from rest_framework import serializers

from .models import (
    SkillCategory,
    ExpertProfile,
    SkillSession,
)
from .marketing_models import SkillMarketingBlock


class SkillCategorySerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    # Annotated by CategoryListView. Feeds the count beside each category in
    # the browse filter rail — the page used to derive it by counting the
    # directory response, which only ever saw the first page of experts.
    expert_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SkillCategory
        fields = ["id", "slug", "label", "icon", "color", "image", "order",
                  "expert_count"]

    def get_image(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class SkillCategoryAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkillCategory
        fields = ["id", "slug", "label", "icon", "color", "image", "order", "is_active"]
        read_only_fields = ["id"]


class SkillMarketingBlockSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = SkillMarketingBlock
        fields = [
            "id", "key", "heading", "subheading", "body",
            "cta_label", "cta_url", "stat_label", "image", "is_active",
        ]
        read_only_fields = ["id", "key"]

    def get_image(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class SkillMarketingBlockAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkillMarketingBlock
        fields = [
            "id", "key", "heading", "subheading", "body",
            "cta_label", "cta_url", "stat_label", "image", "is_active",
        ]
        read_only_fields = ["id", "key"]


class ExpertCardSerializer(serializers.ModelSerializer):
    """Matches a TEACHERS[] entry from data.js, plus location + advertising."""
    name               = serializers.SerializerMethodField()
    title              = serializers.CharField(source="headline")
    # skills / availability: derived from the expert's real listings, not the
    # (often-empty) top-level ExpertProfile columns. See get_skills /
    # get_availability below — the card used to ship skills:[] and
    # availability:"" while listings[] carried real skill_tags + open slots.
    skills             = serializers.SerializerMethodField()
    availability       = serializers.SerializerMethodField()
    cat                = serializers.SerializerMethodField()
    rate               = serializers.IntegerField(source="rate_rupees")
    sessions           = serializers.IntegerField(source="sessions_count")
    img                = serializers.SerializerMethodField()
    teacher_profile_id = serializers.UUIDField(source="teacher_profile.id", read_only=True)

    # Location / offline teaching
    class_mode     = serializers.CharField()
    class_location = serializers.CharField()
    location       = serializers.SerializerMethodField()
    offline        = serializers.SerializerMethodField()

    # Teaching extras
    languages           = serializers.ListField(child=serializers.CharField())
    subject_description = serializers.CharField()

    # Advertising signals (homepage ordering / badges)
    advertised = serializers.SerializerMethodField()
    featured   = serializers.BooleanField(source="is_featured")
    reach      = serializers.IntegerField(source="reach_count")

    # Intro video (advertising clip, not a session recording)
    intro_video_embed_url = serializers.SerializerMethodField()

    reviews_count = serializers.SerializerMethodField()
    my_mastery_progress = serializers.SerializerMethodField()

    # Multi-skill: every bookable offering this expert publishes. A directory
    # row with more than one renders a "from ₹x / Choose a skill" shape rather
    # than a single price + "Book a session".
    listings       = serializers.SerializerMethodField()
    from_rate      = serializers.SerializerMethodField()
    # `from_rate` alone could not say whether it was a floor or a flat price,
    # nor whether zero meant free or merely rounded down. These three carry
    # that, so the card and the profile quote the same thing.
    price_is_from  = serializers.SerializerMethodField()
    is_free        = serializers.SerializerMethodField()
    default_listing_id = serializers.SerializerMethodField()
    open_slots_week = serializers.SerializerMethodField()

    class Meta:
        model = ExpertProfile
        fields = [
            "id", "name", "title", "skills", "cat",
            # Whether this expert is taking NEW requests. Public, because the
            # profile page has to be able to say so BEFORE someone fills in a
            # booking form the server will refuse.
            "accepting_bookings",
            "rating", "sessions", "rate", "img", "bio",
            "badges", "availability", "mastery_target",
            "teacher_profile_id",
            # location
            "class_mode", "class_location", "location", "offline",
            # extras
            "languages", "subject_description",
            "experience_years", "education", "experience_timeline",
            # advertising
            "advertised", "featured", "reach",
            "intro_video_embed_url",
            "reviews_count", "my_mastery_progress",
            # multi-skill
            "listings", "from_rate", "price_is_from", "is_free",
            "default_listing_id", "open_slots_week",
        ]

    def get_listings(self, obj):
        from .listing_serializers import SkillListingCardSerializer
        # Suspended listings are an admin action, not the teacher's — they are
        # hidden outright. Paused ones stay visible but unbookable, which is
        # what the row's "paused by the teacher" line renders.
        rows = [l for l in obj.listings.all() if not l.is_suspended]
        return SkillListingCardSerializer(rows, many=True, context=self.context).data

    def get_skills(self, obj):
        """Union of the expert's own skill_tags and every non-suspended
        listing's skill_tags, de-duplicated, order preserved. The top-level
        ExpertProfile.skill_tags is frequently empty on multi-listing experts,
        so the card would otherwise render no tags at all."""
        seen, out = set(), []
        for tag in (obj.skill_tags or []):
            if tag and tag not in seen:
                seen.add(tag); out.append(tag)
        for l in obj.listings.all():
            if l.is_suspended:
                continue
            for tag in (l.skill_tags or []):
                if tag and tag not in seen:
                    seen.add(tag); out.append(tag)
        return out

    def get_availability(self, obj):
        """Real availability summary. Prefers the expert's own free-text note
        if they set one; otherwise derives a "N slots open this week" string
        from the availability grid, so the card never ships an empty string."""
        if (obj.availability or "").strip():
            return obj.availability
        n = self.get_open_slots_week(obj)
        if n <= 0:
            return "No open slots this week"
        return f"{n} slot{'' if n == 1 else 's'} open this week"

    def _price_paise(self, obj):
        """The paise figure the card should show, and whether it is a floor.

        Returns (paise, is_from). `is_from` is True when the expert publishes
        more than one bookable listing at different prices — the card showed
        `min(listings)` captioned "per 60-min session" with no qualifier, so
        a two-listing expert advertised their cheapest skill as their flat
        rate while the profile page quoted the default listing instead.
        """
        rows = obj.bookable_listings()
        if not rows:
            return obj.hourly_rate or 0, False
        prices = [l.price_paise for l in rows]
        return min(prices), len(set(prices)) > 1

    def get_from_rate(self, obj):
        """Lowest bookable listing price in rupees, falling back to hourly_rate."""
        return self._price_paise(obj)[0] // 100

    def get_price_is_from(self, obj):
        return self._price_paise(obj)[1]

    def get_is_free(self, obj):
        """Free means the price is ZERO, not that it floors to zero rupees.

        `price_paise // 100` turns anything under ₹1 into 0, and the card
        branched on that integer — so a ₹0.50 listing, or a rupee amount
        mistakenly stored unmultiplied, advertised itself as "Free".
        """
        return self._price_paise(obj)[0] == 0

    def get_default_listing_id(self, obj):
        """Which listing the booking form should preselect — and the one the
        server will charge for if the client sends no `listing`."""
        row = obj.default_listing()
        return str(row.id) if row else None

    def get_open_slots_week(self, obj):
        grid = obj.availability_slots or {}
        return len(set(grid.get("open", [])) - set(grid.get("booked", [])))

    def get_reviews_count(self, obj):
        # Annotated by directory_views so a 20-row page costs one query, not 20.
        n = getattr(obj, "public_reviews", None)
        if n is not None:
            return n
        from .review_models import ExpertReview
        return ExpertReview.objects.filter(expert=obj, is_public=True).count()

    def get_my_mastery_progress(self, obj):
        """Completed-session count for the REQUESTING learner, or None when
        there isn't one (public/anonymous read, or no active profile)."""
        request = self.context.get("request")
        if not request:
            return None
        from accounts.auth_flow import get_active_profile
        from .models import mastery_progress
        try:
            learner = get_active_profile(request)
        except Exception:
            return None
        if not learner:
            return None
        return mastery_progress(obj, learner)["progress"]

    def get_name(self, obj):
        return obj.display_name()

    def get_cat(self, obj):
        return obj.category.slug if obj.category_id else None

    def get_advertised(self, obj):
        return obj.is_advertised()

    def get_intro_video_embed_url(self, obj):
        return obj.intro_video_embed_url()

    def get_offline(self, obj):
        return obj.has_offline_class()

    def get_location(self, obj):
        if not (obj.city or obj.district or obj.state or obj.pincode):
            return None
        return {
            "city":     obj.city,
            "district": obj.district,
            "state":    obj.state,
            "pincode":  obj.pincode,
        }

    def get_img(self, obj):
        request = self.context.get("request")
        url = None
        if obj.photo:
            url = obj.photo.url
        else:
            # SELF only — a dependant's photo must never become the
            # expert's public avatar.
            lp = obj.user.self_learner_profile()
            if lp and lp.profile_photo:
                url = lp.profile_photo.url
        if url and request is not None:
            return request.build_absolute_uri(url)
        return url


class SkillSessionSerializer(serializers.ModelSerializer):
    expert = serializers.PrimaryKeyRelatedField(queryset=ExpertProfile.objects.all())

    class Meta:
        model = SkillSession
        fields = [
            "id", "expert", "contact_mode", "status", "scheduled_for",
            "duration_mins", "amount", "note", "meeting_url",
            "payment_status", "created_at",
        ]
        read_only_fields = ["id", "status", "payment_status", "meeting_url", "created_at"]
