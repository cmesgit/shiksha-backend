from rest_framework import serializers
from django.utils import timezone
from django.db import transaction

from accounts.models import Profile
from courses.models import Course

from .models import Enrollment, EnrollmentRequest


class CourseBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ("id", "title", "price")


# -------- Student-facing --------

class EnrollmentRequestCreateSerializer(serializers.ModelSerializer):
    # Profile fields pulled in — optional so existing Profile data is used if omitted,
    # but any value sent here overwrites the Profile (per agreed spec).
    first_name = serializers.CharField(required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(required=False, allow_blank=True, write_only=True)
    phone = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "course",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
            # profile writeback
            "first_name",
            "last_name",
            "phone",
        )
        read_only_fields = ("id",)

    def validate(self, attrs):
        user = self.context["request"].user
        course = attrs["course"]

        if Enrollment.objects.filter(
            user=user, course=course, status=Enrollment.STATUS_ACTIVE
        ).exists():
            raise serializers.ValidationError("You are already enrolled in this course.")

        if EnrollmentRequest.objects.filter(
            user=user, course=course, status=EnrollmentRequest.STATUS_PENDING
        ).exists():
            raise serializers.ValidationError(
                "You already have a pending request for this course."
            )

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user

        profile_fields = {
            "first_name": validated_data.pop("first_name", None),
            "last_name": validated_data.pop("last_name", None),
            "phone": validated_data.pop("phone", None),
        }

        with transaction.atomic():
            profile, _ = Profile.objects.get_or_create(user=user)
            dirty = False
            for key, val in profile_fields.items():
                if val is not None and val != "":
                    setattr(profile, key, val)
                    dirty = True
            if dirty:
                profile.save()

            req = EnrollmentRequest.objects.create(user=user, **validated_data)

        return req


class MyEnrollmentRequestSerializer(serializers.ModelSerializer):
    course = CourseBriefSerializer(read_only=True)
    receipt = serializers.ImageField(read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "course",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
            "status",
            "admin_note",
            "submitted_at",
            "reviewed_at",
        )


# -------- Admin-facing --------

class AdminEnrollmentRequestListSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source="course.title", read_only=True)
    course_price = serializers.IntegerField(source="course.price", read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "user_email",
            "user_name",
            "course_title",
            "course_price",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
            "status",
            "admin_note",
            "submitted_at",
            "reviewed_at",
        )

    def get_user_name(self, obj):
        profile = getattr(obj.user, "profile", None)
        if profile:
            full = f"{profile.first_name} {profile.last_name}".strip()
            if full:
                return full
            if profile.full_name:
                return profile.full_name
        return obj.user.username or obj.user.email


class AdminActionSerializer(serializers.Serializer):
    ACTION_CHOICES = [("approve", "approve"), ("reject", "reject")]

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    admin_note = serializers.CharField(required=False, allow_blank=True)

    def save(self, *, request_obj, reviewer):
        action = self.validated_data["action"]
        note = self.validated_data.get("admin_note", "")

        if request_obj.status != EnrollmentRequest.STATUS_PENDING:
            raise serializers.ValidationError("This request has already been reviewed.")

        with transaction.atomic():
            request_obj.admin_note = note
            request_obj.reviewed_by = reviewer
            request_obj.reviewed_at = timezone.now()

            if action == "approve":
                request_obj.status = EnrollmentRequest.STATUS_APPROVED
                Enrollment.objects.get_or_create(
                    user=request_obj.user,
                    course=request_obj.course,
                    defaults={"status": Enrollment.STATUS_ACTIVE},
                )
            else:
                request_obj.status = EnrollmentRequest.STATUS_REJECTED

            request_obj.save()

        return request_obj
