"""skills/course_serializers.py"""
from rest_framework import serializers
from .course_models import (
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment, SkillLectureProgress,
)


class LectureSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkillCourseLecture
        fields = ["id","title","type","order","video_url","content","duration_sec","is_preview"]
        read_only_fields = ["id"]


class SectionSerializer(serializers.ModelSerializer):
    lectures = LectureSerializer(many=True, read_only=True)
    class Meta:
        model = SkillCourseSection
        fields = ["id","title","order","lectures"]
        read_only_fields = ["id"]


class SkillCourseListSerializer(serializers.ModelSerializer):
    """Card-sized payload for marketplace / teacher lists."""
    teacher_name = serializers.SerializerMethodField()
    teacher_id   = serializers.SerializerMethodField()
    cover        = serializers.SerializerMethodField()
    price_rupees = serializers.IntegerField(read_only=True)
    section_count= serializers.IntegerField(read_only=True)
    lecture_count= serializers.IntegerField(read_only=True)
    is_free      = serializers.BooleanField(read_only=True)

    class Meta:
        model  = SkillCourse
        fields = [
            "id","title","subtitle","level","language","skill_tags",
            "price","price_rupees","is_free",
            "section_count","lecture_count",
            "status","created_at",
            "teacher_name","teacher_id","cover",
        ]

    def get_teacher_name(self, obj):
        # SELF only — this names the course's teacher publicly.
        lp = obj.teacher_profile.user.self_learner_profile()
        if lp: return lp.display_name or lp.full_name or lp.first_name or ""
        return obj.teacher_profile.user.username or ""

    def get_teacher_id(self, obj):
        ep = getattr(obj.teacher_profile, "expert_profile", None)
        return str(ep.id) if ep else None

    def get_cover(self, obj):
        if obj.cover_image:
            req = self.context.get("request")
            return req.build_absolute_uri(obj.cover_image.url) if req else obj.cover_image.url
        return None


class SkillCourseDetailSerializer(SkillCourseListSerializer):
    sections     = SectionSerializer(many=True, read_only=True)
    outcomes     = serializers.JSONField()
    requirements = serializers.JSONField()

    class Meta(SkillCourseListSerializer.Meta):
        fields = SkillCourseListSerializer.Meta.fields + [
            "description","outcomes","requirements",
            "promo_video","reject_reason","sections",
        ]


class SkillCourseWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SkillCourse
        fields = [
            "title","subtitle","description","level","language",
            "skill_tags","price","requirements","outcomes","promo_video",
            "cover_image","category",
        ]


class EnrollmentSerializer(serializers.ModelSerializer):
    course_title = serializers.CharField(source="course.title", read_only=True)
    course_id    = serializers.UUIDField(source="course.id",    read_only=True)

    class Meta:
        model  = SkillCourseEnrollment
        fields = ["id","course_id","course_title","status","enrolled_at","amount_paid"]
        read_only_fields = list(fields)


class LectureProgressSerializer(serializers.ModelSerializer):
    lecture_id = serializers.UUIDField(source="lecture.id", read_only=True)
    class Meta:
        model  = SkillLectureProgress
        fields = ["id","lecture_id","completed_at"]
        read_only_fields = list(fields)
