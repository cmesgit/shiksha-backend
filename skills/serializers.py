"""
skills/serializers.py

Output shapes deliberately match the keys the frontend already reads in
src/components/skill/data.js (TEACHERS / CANDIDATES), so flipping
USE_MOCK=false in skillApi.js needs no component changes.
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
    """Matches a TEACHERS[] entry from data.js."""
    name = serializers.SerializerMethodField()
    title = serializers.CharField(source="headline")
    skills = serializers.ListField(source="skill_tags", child=serializers.CharField())
    cat = serializers.SerializerMethodField()
    rate = serializers.IntegerField(source="rate_rupees")
    sessions = serializers.IntegerField(source="sessions_count")
    img = serializers.SerializerMethodField()

    class Meta:
        model = ExpertProfile
        fields = [
            "id", "name", "title", "skills", "cat",
            "rating", "sessions", "rate", "img", "bio",
            "badges", "availability",
        ]

    def get_name(self, obj):
        return obj.display_name()

    def get_cat(self, obj):
        return obj.category.slug if obj.category_id else None

    def get_img(self, obj):
        request = self.context.get("request")
        url = None
        if obj.photo:
            url = obj.photo.url
        else:
            # user.profile (old model) was removed in 0011; use LearnerProfile.
            lp = obj.user.default_learner_profile()
            if lp and lp.profile_photo:
                url = lp.profile_photo.url
        if url and request is not None:
            return request.build_absolute_uri(url)
        return url


class TeacherApplicationCreateSerializer(serializers.ModelSerializer):
    # Accept a category slug from the frontend; resolve to FK.
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
    name = serializers.SerializerMethodField()
    skill = serializers.CharField(source="skill_name")
    cat = serializers.SerializerMethodField()
    exp = serializers.CharField(source="experience")
    img = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    stage = serializers.CharField(read_only=True)

    class Meta:
        model = TeacherApplication
        fields = ["id", "name", "skill", "cat", "exp", "img", "time", "status", "stage"]

    def get_name(self, obj):
        # user.profile (old model) removed in migration 0011.
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
