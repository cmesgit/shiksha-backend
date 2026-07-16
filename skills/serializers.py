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
    TeacherApplication,
    InterviewSlot,
    Evaluation,
    SkillSession,
)


class SkillCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SkillCategory
        fields = ["id", "slug", "label", "icon", "color"]


class ExpertCardSerializer(serializers.ModelSerializer):
    """Matches a TEACHERS[] entry from data.js, plus location + advertising."""
    name               = serializers.SerializerMethodField()
    title              = serializers.CharField(source="headline")
    skills             = serializers.ListField(source="skill_tags", child=serializers.CharField())
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

    class Meta:
        model = ExpertProfile
        fields = [
            "id", "name", "title", "skills", "cat",
            "rating", "sessions", "rate", "img", "bio",
            "badges", "availability",
            "teacher_profile_id",
            # location
            "class_mode", "class_location", "location", "offline",
            # extras
            "languages", "subject_description",
            # advertising
            "advertised", "featured", "reach",
            "intro_video_embed_url",
        ]

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
            lp = obj.user.default_learner_profile()
            if lp and lp.profile_photo:
                url = lp.profile_photo.url
        if url and request is not None:
            return request.build_absolute_uri(url)
        return url


class TeacherApplicationCreateSerializer(serializers.ModelSerializer):
    category = serializers.SlugRelatedField(
        slug_field="slug", queryset=SkillCategory.objects.all(),
        required=False, allow_null=True,
    )

    class Meta:
        model = TeacherApplication
        fields = [
            "id", "track", "category", "skill_name", "headline",
            "experience", "method_note", "skill_tags", "intro_video", "status",
        ]
        read_only_fields = ["id", "status"]


class InterviewSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = InterviewSlot
        fields = ["id", "starts_at", "duration_mins"]


class ReviewQueueSerializer(serializers.ModelSerializer):
    """Matches a CANDIDATES[] entry from data.js."""
    name  = serializers.SerializerMethodField()
    skill = serializers.CharField(source="skill_name")
    cat   = serializers.SerializerMethodField()
    exp   = serializers.CharField(source="experience")
    img   = serializers.SerializerMethodField()
    time  = serializers.SerializerMethodField()
    stage = serializers.CharField(read_only=True)

    class Meta:
        model = TeacherApplication
        fields = ["id", "name", "skill", "cat", "exp", "img", "time", "status", "stage"]

    def get_name(self, obj):
        lp = obj.user.default_learner_profile()
        if lp:
            name = f"{lp.first_name} {lp.last_name}".strip() or lp.full_name or lp.display_name
            if name:
                return name
        return obj.user.username or obj.user.email

    def get_cat(self, obj):
        return obj.category.label if obj.category_id else ""

    def get_img(self, obj):
        request = self.context.get("request")
        lp = obj.user.default_learner_profile()
        if lp and lp.profile_photo:
            url = lp.profile_photo.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_time(self, obj):
        interview = getattr(obj, "interview", None)
        return interview.scheduled_for if interview else None


class EvaluationSerializer(serializers.ModelSerializer):
    tier = serializers.CharField(source="recommended_tier", required=False, allow_blank=True)

    class Meta:
        model = Evaluation
        fields = ["id", "scores", "decision", "tier", "feedback", "created_at"]
        read_only_fields = ["id", "created_at"]


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
